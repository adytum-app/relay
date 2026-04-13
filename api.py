"""
Adytum Backend API

FastAPI server that:
1. Receives execution/purchase requests from frontend
2. Forwards to TEE worker
3. Returns results to frontend
4. Proxies IPFS metadata

The TEE worker handles on-chain submission.

Matches AdytumMarketplace.sol contract interface.
"""

import os
import re
from datetime import datetime
from typing import Optional, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from web3 import Web3
import httpx

# =============================================================================
# Configuration
# =============================================================================

RPC_URL = os.getenv("RPC_URL", "https://sepolia.base.org")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")
TEE_WORKER_URL = os.getenv("TEE_WORKER_URL", "http://localhost:8001")
IPFS_GATEWAY = os.getenv(
    "IPFS_GATEWAY",
    "https://olive-useful-fly-746.mypinata.cloud/",
)
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

# =============================================================================
# Contract Constants (must match AdytumMarketplace.sol)
# =============================================================================


class MonetizationModel:
    PAY_PER_USE = 0
    NASH_NEGOTIATION = 1


class InventionCategory:
    UNCATEGORIZED = 0
    DATA_SCIENCE = 1
    MACHINE_LEARNING = 2
    FINANCIAL = 3
    OPTIMIZATION = 4
    SIMULATION = 5
    OTHER = 6


class NashPhase:
    OPEN = 0
    REVEAL = 1
    SETTLED = 2
    FAILED = 3
    EXPIRED = 4


# =============================================================================
# In-Memory Storage (use Redis/Postgres in production)
# =============================================================================

# NOTE: We intentionally do NOT cache on-chain state (inventions, Nash configs,
# usage stats) because this data changes on-chain and caching would cause
# stale reads (e.g., Nash phase stuck on OPEN after deadline passes).
# Only cache execution results which are transient API-side state.

executions_cache: dict[str, dict] = {}

# =============================================================================
# Pydantic Validators
# =============================================================================


def validate_bytes32(v: str) -> str:
    """Validate and normalize bytes32 hex string."""
    clean = v.replace("0x", "")
    if not re.match(r"^[a-fA-F0-9]{64}$", clean):
        raise ValueError("Must be a valid bytes32 hex string")
    return "0x" + clean.lower()


def validate_address(v: str) -> str:
    """Validate and normalize Ethereum address."""
    if not re.match(r"^0x[a-fA-F0-9]{40}$", v):
        raise ValueError("Must be a valid Ethereum address")
    return v.lower()


# =============================================================================
# Request/Response Models
# =============================================================================

# --- Execution (Pay-Per-Use) ---

class ExecuteRequestBody(BaseModel):
    """Request to execute invention code (Pay-Per-Use model)."""
    execution_id: str = Field(
        ..., description="Unique execution ID (bytes32 hex)"
    )
    invention_id: str = Field(..., description="Invention ID (bytes32 hex)")
    buyer: str = Field(..., description="Buyer address")
    input_data: dict = Field(..., description="Input data for the invention")

    @field_validator("execution_id", "invention_id")
    @classmethod
    def _validate_bytes32(cls, v: str) -> str:
        return validate_bytes32(v)

    @field_validator("buyer")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return validate_address(v)


class ExecuteResponse(BaseModel):
    """Response from execution request."""
    execution_id: str
    status: str  # "pending", "executing", "completed", "failed"
    output: Optional[Any] = None
    result_hash: Optional[str] = None
    execution_time_ms: Optional[int] = None
    attestation: Optional[str] = None
    error: Optional[str] = None


# --- Key Release (Nash Winner) ---

class ReleaseKeyRequestBody(BaseModel):
    """Request to release decryption key to Nash winner."""
    invention_id: str = Field(..., description="Invention ID (bytes32 hex)")
    buyer: str = Field(..., description="Buyer address (must be Nash winner)")

    @field_validator("invention_id")
    @classmethod
    def _validate_bytes32(cls, v: str) -> str:
        return validate_bytes32(v)

    @field_validator("buyer")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return validate_address(v)


class ReleaseKeyResponse(BaseModel):
    """Response from key release request."""
    invention_id: str
    buyer: str
    status: str  # "pending", "released", "failed"
    encrypted_key: Optional[str] = None
    attestation: Optional[str] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None


# --- Store Key (Seller Listing) ---

class StoreKeyRequestBody(BaseModel):
    """Request to store decryption key in TEE."""
    invention_id: str = Field(..., description="Invention ID (bytes32 hex)")
    decryption_key: str = Field(
        ..., description="Fernet decryption key (44 chars)"
    )
    seller: str = Field(..., description="Seller address")

    @field_validator("invention_id")
    @classmethod
    def _validate_bytes32(cls, v: str) -> str:
        return validate_bytes32(v)

    @field_validator("seller")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return validate_address(v)

    @field_validator("decryption_key")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        if len(v) != 44:
            raise ValueError(
                "Decryption key must be a valid Fernet key (44 chars)"
            )
        return v


class StoreKeyResponse(BaseModel):
    """Response from store key request."""
    success: bool
    invention_id: str
    error: Optional[str] = None


# --- Invention Metadata ---

class InventionMetadata(BaseModel):
    """Metadata for an invention (stored on IPFS)."""
    name: str
    description: str
    image: Optional[str] = None
    encryptedCodeUri: str
    inputSchema: Optional[dict] = None
    outputSchema: Optional[dict] = None
    sampleInput: Optional[dict] = None
    sampleOutput: Optional[dict] = None
    benchmarks: Optional[list[dict]] = None
    tags: Optional[list[str]] = None


# --- Encryption Helper ---

class EncryptCodeRequest(BaseModel):
    """Request to encrypt invention code."""
    code: str = Field(..., description="Python code to encrypt")


class EncryptCodeResponse(BaseModel):
    """Response with encrypted code and hashes."""
    encrypted_code: str  # Base64 Fernet-encrypted
    decryption_key: str  # Fernet key (44 chars)
    encrypted_code_hash: str  # keccak256 of encrypted code
    encryption_key_hash: str  # keccak256 of decryption key


# =============================================================================
# Web3 Setup
# =============================================================================

w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Full ABI matching AdytumMarketplace.sol
CONTRACT_ABI = [
    # --- View Functions ---
    {
        "name": "getInvention",
        "type": "function",
        "inputs": [{"name": "id", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "id", "type": "bytes32"},
                    {"name": "seller", "type": "address"},
                    {"name": "metadataURI", "type": "string"},
                    {"name": "encryptedCodeHash", "type": "bytes32"},
                    {"name": "encryptionKeyHash", "type": "bytes32"},
                    {"name": "category", "type": "uint8"},
                    {"name": "model", "type": "uint8"},
                    {"name": "createdAt", "type": "uint256"},
                    {"name": "isActive", "type": "bool"},
                ]
            }
        ],
        "stateMutability": "view",
    },
    {
        "name": "getPayPerUseConfig",
        "type": "function",
        "inputs": [{"name": "inventionId", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "pricePerCall", "type": "uint128"},
                    {"name": "maxCallsPerDay", "type": "uint64"},
                    {"name": "maxCallsPer30Days", "type": "uint64"},
                    {"name": "cooldownSeconds", "type": "uint32"},
                    {"name": "totalCalls", "type": "uint256"},
                    {"name": "totalRevenue", "type": "uint256"},
                ]
            }
        ],
        "stateMutability": "view",
    },
    {
        "name": "getNashConfig",
        "type": "function",
        "inputs": [{"name": "inventionId", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "sellerBidHash", "type": "bytes32"},
                    {"name": "sellerMinRevealed", "type": "uint256"},
                    {"name": "bidDeadline", "type": "uint256"},
                    {"name": "revealDeadline", "type": "uint256"},
                    {"name": "requiredDeposit", "type": "uint256"},
                    {"name": "sellerRevealed", "type": "bool"},
                    {"name": "allowTrialsDuring", "type": "bool"},
                    {"name": "trialFee", "type": "uint256"},
                    {"name": "maxTrialsPerBidder", "type": "uint256"},
                    {"name": "phase", "type": "uint8"},
                    {"name": "highestBidder", "type": "address"},
                    {"name": "highestBid", "type": "uint256"},
                    {"name": "sellerBond", "type": "uint256"},
                ]
            }
        ],
        "stateMutability": "view",
    },
    {
        "name": "getBuyerUsage",
        "type": "function",
        "inputs": [
            {"name": "inventionId", "type": "bytes32"},
            {"name": "buyer", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "callsToday", "type": "uint64"},
                    {"name": "callsThis30Days", "type": "uint64"},
                    {"name": "lastCallTime", "type": "uint64"},
                    {"name": "lastDayReset", "type": "uint64"},
                    {"name": "last30DayReset", "type": "uint64"},
                ]
            }
        ],
        "stateMutability": "view",
    },
    {
        "name": "getNashBid",
        "type": "function",
        "inputs": [
            {"name": "inventionId", "type": "bytes32"},
            {"name": "bidder", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "bidHash", "type": "bytes32"},
                    {"name": "revealedAmount", "type": "uint256"},
                    {"name": "depositAmount", "type": "uint256"},
                    {"name": "buyerPubKey", "type": "bytes"},
                    {"name": "submitted", "type": "bool"},
                    {"name": "revealed", "type": "bool"},
                    {"name": "depositForfeited", "type": "bool"},
                    {"name": "trialCount", "type": "uint256"},
                ]
            }
        ],
        "stateMutability": "view",
    },
    {
        "name": "inventionCount",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]


def get_contract():
    """Get contract instance."""
    if not CONTRACT_ADDRESS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CONTRACT_ADDRESS not configured"
        )
    return w3.eth.contract(address=CONTRACT_ADDRESS, abi=CONTRACT_ABI)


# =============================================================================
# FastAPI App
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    print("🔮 Adytum Backend API starting...")
    print(f"   RPC: {RPC_URL}")
    print(f"   Contract: {CONTRACT_ADDRESS}")
    print(f"   TEE Worker: {TEE_WORKER_URL}")
    print(f"   IPFS Gateway: {IPFS_GATEWAY}")
    yield
    print("🔮 Adytum Backend API shutting down...")


app = FastAPI(
    title="Adytum Marketplace API",
    description=(
        "Backend API for the Adytum TEE-protected IP marketplace. "
        "Implements NDAi paper (Stephenson et al., 2025)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# =============================================================================
# Health & Status
# =============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    # Check TEE worker health
    tee_status = "unknown"
    tee_oracle = None

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{TEE_WORKER_URL}/health")
            if response.status_code == 200:
                tee_data = response.json()
                tee_status = tee_data.get("status", "unknown")
                tee_oracle = tee_data.get("oracle_address")
    except Exception:
        tee_status = "unreachable"

    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "contract_address": CONTRACT_ADDRESS,
        "tee_worker": {
            "url": TEE_WORKER_URL,
            "status": tee_status,
            "oracle_address": tee_oracle,
        },
    }


# =============================================================================
# Execution Endpoints (Pay-Per-Use)
# =============================================================================

@app.post("/api/execute", response_model=ExecuteResponse)
async def request_execution(
    body: ExecuteRequestBody, background_tasks: BackgroundTasks
):
    """
    Request code execution (Pay-Per-Use model).

    Frontend calls this after the on-chain requestExecution() tx confirms.
    This triggers the TEE to execute the invention in the nsjail sandbox.

    Flow:
    1. Frontend calls contract.requestExecution() → emits ExecutionRequested
    2. Frontend calls this endpoint with execution_id
    3. Backend forwards to TEE worker
    4. TEE fetches code, decrypts, executes in sandbox
    5. TEE submits result hash on-chain
    6. Backend returns result to frontend
    """
    execution_id = body.execution_id

    # Check if execution already exists
    if execution_id in executions_cache:
        cached = executions_cache[execution_id]
        return ExecuteResponse(
            execution_id=execution_id,
            status=cached["status"],
            **cached.get("result", {}),
        )

    # Initialize in cache
    executions_cache[execution_id] = {
        "status": "pending",
        "invention_id": body.invention_id,
        "buyer": body.buyer,
        "input_data": body.input_data,
        "created_at": datetime.utcnow().isoformat(),
    }

    # Execute in background
    background_tasks.add_task(execute_background, execution_id, body)

    return ExecuteResponse(execution_id=execution_id, status="pending")


async def execute_background(execution_id: str, body: ExecuteRequestBody):
    """Background task to execute via TEE worker."""
    try:
        executions_cache[execution_id]["status"] = "executing"

        # Call TEE worker
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{TEE_WORKER_URL}/execute",
                json={
                    "execution_id": body.execution_id,
                    "invention_id": body.invention_id,
                    "buyer": body.buyer,
                    "input_data": body.input_data,
                },
            )
            response.raise_for_status()
            result = response.json()

        if result.get("success"):
            executions_cache[execution_id]["status"] = "completed"
            executions_cache[execution_id]["result"] = {
                "output": result.get("output"),
                "result_hash": result.get("result_hash"),
                "execution_time_ms": result.get("execution_time_ms"),
                "attestation": result.get("attestation"),
            }
        else:
            executions_cache[execution_id]["status"] = "failed"
            executions_cache[execution_id]["result"] = {
                "error": result.get("error", "Unknown error"),
            }

    except httpx.HTTPStatusError as e:
        executions_cache[execution_id]["status"] = "failed"
        executions_cache[execution_id]["result"] = {
            "error": f"TEE worker error: {e.response.status_code}",
        }
    except Exception as e:
        executions_cache[execution_id]["status"] = "failed"
        executions_cache[execution_id]["result"] = {"error": str(e)}


@app.get("/api/execute/{execution_id}", response_model=ExecuteResponse)
async def get_execution_status(execution_id: str):
    """Get the status of an execution."""
    # Validate format
    try:
        execution_id = validate_bytes32(execution_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid execution ID format"
        )

    if execution_id not in executions_cache:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found"
        )

    cached = executions_cache[execution_id]
    return ExecuteResponse(
        execution_id=execution_id,
        status=cached["status"],
        **cached.get("result", {}),
    )


# =============================================================================
# Key Management Endpoints
# =============================================================================

@app.post("/api/keys/store", response_model=StoreKeyResponse)
async def store_key(body: StoreKeyRequestBody):
    """
    Store decryption key in TEE.

    Called by seller when listing an invention:
    1. Seller encrypts code with Fernet key
    2. Seller uploads encrypted code to IPFS
    3. Seller calls this endpoint to store key in TEE
    4. Seller calls contract.listPayPerUse() or contract.listNashNegotiation()
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{TEE_WORKER_URL}/store-key",
                json={
                    "invention_id": body.invention_id,
                    "decryption_key": body.decryption_key,
                    "seller": body.seller,
                },
            )
            response.raise_for_status()
            result = response.json()

        return StoreKeyResponse(
            success=result.get("success", False),
            invention_id=body.invention_id,
            error=result.get("error"),
        )

    except httpx.HTTPStatusError as e:
        return StoreKeyResponse(
            success=False,
            invention_id=body.invention_id,
            error=f"TEE worker error: {e.response.status_code}",
        )
    except Exception as e:
        return StoreKeyResponse(
            success=False,
            invention_id=body.invention_id,
            error=str(e),
        )


@app.post("/api/keys/release", response_model=ReleaseKeyResponse)
async def release_key(body: ReleaseKeyRequestBody):
    """
    Release decryption key to Nash winner.

    Called by buyer after Nash negotiation settles:
    1. Buyer wins Nash negotiation (highest valid bid)
    2. Seller reveals and settles the Nash auction
    3. Buyer calls this endpoint to get encrypted key
    4. TEE verifies buyer is winner, encrypts key with buyer's pubkey
    5. Buyer decrypts key locally with their private key
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{TEE_WORKER_URL}/release-key",
                json={
                    "invention_id": body.invention_id,
                    "buyer": body.buyer,
                },
            )
            response.raise_for_status()
            result = response.json()

        if result.get("success"):
            return ReleaseKeyResponse(
                invention_id=body.invention_id,
                buyer=body.buyer,
                status="released",
                encrypted_key=result.get("encrypted_key"),
                attestation=result.get("attestation"),
                tx_hash=result.get("tx_hash"),
            )
        else:
            return ReleaseKeyResponse(
                invention_id=body.invention_id,
                buyer=body.buyer,
                status="failed",
                error=result.get("error"),
            )

    except httpx.HTTPStatusError as e:
        return ReleaseKeyResponse(
            invention_id=body.invention_id,
            buyer=body.buyer,
            status="failed",
            error=f"TEE worker error: {e.response.status_code}",
        )
    except Exception as e:
        return ReleaseKeyResponse(
            invention_id=body.invention_id,
            buyer=body.buyer,
            status="failed",
            error=str(e),
        )


@app.get("/api/keys/{invention_id}/exists")
async def check_key_exists(invention_id: str):
    """Check if a decryption key exists in TEE for an invention."""
    try:
        invention_id = validate_bytes32(invention_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid invention ID format"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{TEE_WORKER_URL}/keys/{invention_id}"
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"invention_id": invention_id, "key_exists": False}
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="TEE worker error"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e)
        )


# =============================================================================
# Invention Endpoints
# =============================================================================

@app.get("/api/inventions/{invention_id}")
async def get_invention(invention_id: str):
    """
    Get invention details from contract.

    NOTE: We always fetch fresh from the blockchain to ensure
    Nash phase and other state is current. Do not cache this data.
    """
    try:
        invention_id = validate_bytes32(invention_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid invention ID format"
        )

    # Always fetch fresh from contract (no caching - state changes on-chain)
    try:
        contract = get_contract()
        inv_bytes = bytes.fromhex(invention_id.replace("0x", ""))
        result = contract.functions.getInvention(inv_bytes).call()

        invention = {
            "id": "0x" + result[0].hex(),
            "seller": result[1],
            "metadataURI": result[2],
            "encryptedCodeHash": "0x" + result[3].hex(),
            "encryptionKeyHash": "0x" + result[4].hex(),
            "category": result[5],
            "model": result[6],
            "createdAt": result[7],
            "isActive": result[8],
        }

        # Fetch model-specific config
        if invention["model"] == MonetizationModel.PAY_PER_USE:
            ppu_result = contract.functions.getPayPerUseConfig(
                inv_bytes
            ).call()
            invention["payPerUseConfig"] = {
                "pricePerCall": str(ppu_result[0]),
                "maxCallsPerDay": ppu_result[1],
                "maxCallsPer30Days": ppu_result[2],
                "cooldownSeconds": ppu_result[3],
                "totalCalls": ppu_result[4],
                "totalRevenue": str(ppu_result[5]),
            }
        elif invention["model"] == MonetizationModel.NASH_NEGOTIATION:
            nash_result = contract.functions.getNashConfig(inv_bytes).call()
            invention["nashConfig"] = {
                "sellerBidHash": "0x" + nash_result[0].hex(),
                "sellerMinRevealed": str(nash_result[1]),
                "bidDeadline": nash_result[2],
                "revealDeadline": nash_result[3],
                "requiredDeposit": str(nash_result[4]),
                "sellerRevealed": nash_result[5],
                "allowTrialsDuring": nash_result[6],
                "trialFee": str(nash_result[7]),
                "maxTrialsPerBidder": nash_result[8],
                "phase": nash_result[9],
                "highestBidder": nash_result[10],
                "highestBid": str(nash_result[11]),
                "sellerBond": str(nash_result[12]),
            }

        return invention

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invention not found: {e}"
        )


@app.get("/api/inventions/{invention_id}/usage/{buyer}")
async def get_buyer_usage(invention_id: str, buyer: str):
    """Get buyer's usage stats for an invention."""
    try:
        invention_id = validate_bytes32(invention_id)
        buyer = validate_address(buyer)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    try:
        contract = get_contract()
        inv_bytes = bytes.fromhex(invention_id.replace("0x", ""))
        result = contract.functions.getBuyerUsage(inv_bytes, buyer).call()

        return {
            "inventionId": invention_id,
            "buyer": buyer,
            "callsToday": result[0],
            "callsThis30Days": result[1],
            "lastCallTime": result[2],
            "lastDayReset": result[3],
            "last30DayReset": result[4],
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usage not found: {e}"
        )


@app.get("/api/inventions/{invention_id}/bid/{bidder}")
async def get_nash_bid(invention_id: str, bidder: str):
    """Get bidder's Nash bid for an invention."""
    try:
        invention_id = validate_bytes32(invention_id)
        bidder = validate_address(bidder)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    try:
        contract = get_contract()
        inv_bytes = bytes.fromhex(invention_id.replace("0x", ""))
        result = contract.functions.getNashBid(inv_bytes, bidder).call()

        return {
            "inventionId": invention_id,
            "bidder": bidder,
            "bidHash": "0x" + result[0].hex(),
            "revealedAmount": str(result[1]),
            "depositAmount": str(result[2]),
            "buyerPubKey": "0x" + result[3].hex() if result[3] else None,
            "submitted": result[4],
            "revealed": result[5],
            "depositForfeited": result[6],
            "trialCount": result[7],
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bid not found: {e}"
        )


# =============================================================================
# IPFS Proxy Endpoints
# =============================================================================

@app.get("/api/ipfs/{cid}")
async def get_ipfs_content(cid: str):
    """Fetch and proxy IPFS content."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{IPFS_GATEWAY}{cid}")
            response.raise_for_status()

            # Try to parse as JSON
            try:
                return response.json()
            except:
                return {"content": response.text}

    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail="Failed to fetch from IPFS"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"IPFS gateway error: {e}"
        )


# =============================================================================
# Encryption Helper Endpoints (for sellers)
# =============================================================================

@app.post("/api/encrypt", response_model=EncryptCodeResponse)
async def encrypt_code(body: EncryptCodeRequest):
    """
    Encrypt invention code for upload.

    Returns:
    - encrypted_code: Base64-encoded Fernet-encrypted code
    - decryption_key: Fernet key (store in TEE via /api/keys/store)
    - encrypted_code_hash: keccak256 hash (submit to contract)
    - encryption_key_hash: keccak256 hash (submit to contract)

    In production, this should happen client-side for security.
    This endpoint is provided for convenience during development.
    """
    from cryptography.fernet import Fernet

    # Generate encryption key
    key = Fernet.generate_key()
    fernet = Fernet(key)

    # Encrypt the code
    encrypted = fernet.encrypt(body.code.encode())

    # Compute hashes (using keccak256 to match contract)
    encrypted_code_hash = "0x" + Web3.keccak(encrypted).hex()
    encryption_key_hash = "0x" + Web3.keccak(key).hex()

    return EncryptCodeResponse(
        encrypted_code=encrypted.decode(),  # Base64
        decryption_key=key.decode(),
        encrypted_code_hash=encrypted_code_hash,
        encryption_key_hash=encryption_key_hash,
    )


# =============================================================================
# Nash Bid Hash Helper
# =============================================================================

@app.post("/api/nash/generate-bid-hash")
async def generate_bid_hash(amount: int, salt: str):
    """
    Generate a Nash bid hash.

    The hash is keccak256(abi.encodePacked(amount, salt)).
    This matches the contract's expected format.
    """
    try:
        salt_bytes = bytes.fromhex(salt.replace("0x", ""))
        if len(salt_bytes) != 32:
            raise ValueError("Salt must be 32 bytes")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Salt must be a valid bytes32 hex string"
        )

    # Pack amount (uint256) + salt (bytes32)
    packed = Web3.solidity_keccak(
        ["uint256", "bytes32"],
        [amount, salt_bytes]
    )

    return {
        "bidHash": "0x" + packed.hex(),
        "amount": amount,
        "salt": "0x" + salt_bytes.hex(),
    }


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    print(f"Starting Adytum Backend API on {host}:{port}")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )

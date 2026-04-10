# Adytum Backend API/Relay

> _The bridge between frontend and TEE_

FastAPI server that connects the Adytum frontend to the TEE worker, handling execution requests, key management, and contract queries.

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109-green)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Architecture

```
┌─────────────┐      ┌─────────────────┐      ┌─────────────────┐
│   Frontend  │ ──── │  Backend API    │ ──── │   TEE Worker    │
│  (Next.js)  │      │   (FastAPI)     │      │  (nsjail/dstack)│
└─────────────┘      └────────┬────────┘      └─────────────────┘
                              │
                              ▼
                     ┌─────────────────-┐
                     │ AdytumMarketplace│
                     │   (Base Sepolia) │
                     └─────────────────-┘
```

---

## Features

- **Execution proxy** — forwards Pay-Per-Use execution requests to TEE worker
- **Key management** — store/release decryption keys via TEE
- **Contract queries** — fetch invention details, usage stats, Nash bids
- **IPFS proxy** — fetch metadata from IPFS gateway
- **Encryption helper** — encrypt code for listing (dev convenience)
- **Nash bid hash** — generate bid hashes matching contract format

---

## API Endpoints

### Health

```bash
GET /health
→ { status, timestamp, contract_address, tee_worker: { url, status, oracle_address } }
```

### Execution (Pay-Per-Use)

```bash
# Request execution
POST /api/execute
{
  "execution_id": "0x...",
  "invention_id": "0x...",
  "buyer": "0x...",
  "input_data": { ... }
}
→ { execution_id, status, output?, result_hash?, attestation? }

# Poll execution status
GET /api/execute/{execution_id}
→ { execution_id, status, output?, result_hash?, attestation?, error? }
```

### Key Management

```bash
# Store key (seller listing)
POST /api/keys/store
{
  "invention_id": "0x...",
  "decryption_key": "...",  # Fernet key (44 chars)
  "seller": "0x..."
}
→ { success, invention_id }

# Release key (Nash winner)
POST /api/keys/release
{
  "invention_id": "0x...",
  "buyer": "0x..."
}
→ { invention_id, buyer, status, encrypted_key?, attestation?, tx_hash? }

# Check key exists
GET /api/keys/{invention_id}/exists
→ { invention_id, key_exists }
```

### Inventions

```bash
# Get invention details
GET /api/inventions/{invention_id}
→ { id, seller, metadataURI, model, payPerUseConfig?, nashConfig?, ... }

# Get buyer usage
GET /api/inventions/{invention_id}/usage/{buyer}
→ { callsToday, callsThis30Days, lastCallTime, ... }

# Get Nash bid
GET /api/inventions/{invention_id}/bid/{bidder}
→ { bidHash, revealedAmount, depositAmount, submitted, revealed, ... }
```

### IPFS Proxy

```bash
GET /api/ipfs/{cid}
→ { ...metadata }
```

### Helpers

```bash
# Encrypt code (for sellers)
POST /api/encrypt
{ "code": "def run(input_data): ..." }
→ { encrypted_code, decryption_key, encrypted_code_hash, encryption_key_hash }

# Generate Nash bid hash
POST /api/nash/generate-bid-hash?amount=1000000&salt=0x...
→ { bidHash, amount, salt }
```

---

## Environment Variables

| Variable           | Required | Default                    | Description                            |
| ------------------ | -------- | -------------------------- | -------------------------------------- |
| `CONTRACT_ADDRESS` | Yes      | -                          | AdytumMarketplace contract address     |
| `TEE_WORKER_URL`   | No       | `http://localhost:8001`    | TEE worker URL                         |
| `RPC_URL`          | No       | `https://sepolia.base.org` | Base RPC endpoint                      |
| `IPFS_GATEWAY`     | No       | `https://ipfs.io/ipfs/`    | IPFS gateway URL                       |
| `HOST`             | No       | `0.0.0.0`                  | Server bind address                    |
| `PORT`             | No       | `8000`                     | Server port                            |
| `CORS_ORIGINS`     | No       | `*`                        | Allowed CORS origins (comma-separated) |

---

## Development

### Local

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment
export CONTRACT_ADDRESS=0x...
export TEE_WORKER_URL=http://localhost:8001

# Run server
python api.py
```

### Docker

```bash
# Build
docker build -t adytum-backend .

# Run
docker run -p 8000:8000 \
  -e CONTRACT_ADDRESS=0x... \
  -e TEE_WORKER_URL=http://tee-worker:8001 \
  adytum-backend
```

### Docker Compose (with TEE Worker)

```yaml
version: "3.8"

services:
  backend:
    build: ./backend
    ports:
      - "8000:8000"
    environment:
      - CONTRACT_ADDRESS=0x...
      - TEE_WORKER_URL=http://tee-worker:8001
      - RPC_URL=https://sepolia.base.org
    depends_on:
      - tee-worker

  tee-worker:
    build: ./tee-worker
    ports:
      - "8001:8001"
    environment:
      - CONTRACT_ADDRESS=0x...
      - ORACLE_PRIVATE_KEY=0x...
      - RPC_URL=https://sepolia.base.org
    cap_add:
      - SYS_ADMIN
      - SYS_PTRACE
```

---

## Contract Integration

The backend queries the AdytumMarketplace contract for:

| Function                   | Purpose                   |
| -------------------------- | ------------------------- |
| `getInvention(id)`         | Fetch invention details   |
| `getPayPerUseConfig(id)`   | Fetch PPU pricing/limits  |
| `getNashConfig(id)`        | Fetch Nash auction state  |
| `getBuyerUsage(id, buyer)` | Fetch buyer's usage stats |
| `getNashBid(id, bidder)`   | Fetch bidder's Nash bid   |

---

## Flow: Pay-Per-Use Execution

```
Frontend                    Backend                     TEE Worker
   │                           │                            │
   │ contract.requestExecution │                            │
   │ ─────────────────────────>│                            │
   │                           │                            │
   │ POST /api/execute         │                            │
   │ ─────────────────────────>│                            │
   │                           │                            │
   │                           │ POST /execute              │
   │                           │ ──────────────────────────>│
   │                           │                            │
   │                           │<── { output, attestation } │
   │                           │                            │
   │<── { status: "pending" }  │                            │
   │                           │                            │
   │ GET /api/execute/{id}     │                            │
   │ (polling)                 │                            │
   │ ─────────────────────────>│                            │
   │                           │                            │
   │<── { status: "completed", │                            │
   │      output, attestation }│                            │
```

---

## Flow: Nash Key Release

```
Frontend                    Backend                     TEE Worker
   │                           │                            │
   │ (Nash settles on-chain)   │                            │
   │                           │                            │
   │ POST /api/keys/release    │                            │
   │ ─────────────────────────>│                            │
   │                           │                            │
   │                           │ POST /release-key          │
   │                           │ ──────────────────────────>│
   │                           │                            │
   │                           │ [Verify phase=SETTLED]     │
   │                           │ [Verify buyer=winner]      │
   │                           │ [ECIES encrypt key]        │
   │                           │                            │
   │                           │<── { encrypted_key, attest}│
   │                           │                            │
   │<── { encrypted_key,       │                            │
   │      attestation, tx_hash}│                            │
```

---

## License

MIT License - see [LICENSE](LICENSE) for details.

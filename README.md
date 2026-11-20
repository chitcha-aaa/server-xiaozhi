# XiaoZhi ESP32 ↔ LangMind Bridge Server

เซิร์ฟเวอร์กลางสำหรับเชื่อมต่อบอร์ด XiaoZhi ESP32 หลายเครื่องเข้ากับ LangMind Agent ผ่าน WebSocket รองรับการจัดการอุปกรณ์หลายบอร์ด (Multi-board), โปรไฟล์ฮาร์ดแวร์/VAD แยกตามรุ่นบอร์ด, หน้า **Admin Dashboard (`/admin`)** สำหรับอนุมัติและตั้งค่าบอร์ดผ่าน PostgreSQL และการแสดงภาพสถานะหน้าจอ

## ความสามารถหลัก

- **Voice Bridge (`/ws`)**: รับเสียง Opus จากบอร์ด ตรวจจับช่วงหยุดพูดด้วย VAD ส่งต่อให้ LangMind (STT → Agent → TTS) และสตรีมเสียงตอบกลับแบบเรียลไทม์ พร้อมรองรับการตัดบท (interrupt)
- **OTA Provisioning (`/ota`)**: จ่ายค่า WebSocket URL, เวลาเซิร์ฟเวอร์ และโทเค็นอุปกรณ์เมื่อบอร์ดบูต (รองรับการลงทะเบียนบอร์ดใหม่อัตโนมัติเข้าคิวรออนุมัติเมื่อเปิดใช้ PostgreSQL)
- **Multi-board & Hardware Profiles**: แยกการตั้งค่า Audio, VAD, หน้าจอ และ MCP ตามโปรไฟล์ เช่น `default` และ `lynai` (ดูรายละเอียดเพิ่มเติมใน [`docs/BOARD2.md`](docs/BOARD2.md))
- **Admin Dashboard (`/admin`)**: เว็บแดชบอร์ด (React + Vite) สำหรับทีมภายใน ล็อกอินด้วย `ADMIN_PASSWORD` เพื่ออนุมัติ/ระงับบอร์ด เปลี่ยนชื่อ เลือกโปรไฟล์ และปรับแต่งค่า VAD/Screen
- **Screen & Snapshot**: เรนเดอร์ภาพสถานะภาษาไทย/อังกฤษ (`/screen/{profile}/{slot}.png`) และรับภาพหน้าจอจากบอร์ดผ่าน MCP (`/screen/upload`, `/debug/snapshot`)

## โครงสร้างโปรเจกต์

```text
.
├── app/                   # โค้ด FastAPI server, WebSocket session, VAD, LangMind client, Admin API
│   ├── server.py          # จุดเริ่มต้นของแอป (uvicorn app.server:app)
│   ├── session.py         # จัดการ WebSocket session และวงจรรับ-ส่งเสียงต่อบอร์ด
│   ├── admin_api.py       # REST API สำหรับ Admin Dashboard (/api/admin/*)
│   ├── device_store.py    # จัดเก็บอุปกรณ์และโปรไฟล์ใน PostgreSQL
│   ├── devices.py         # Device registry & profile loader (fallback เมื่อไม่ใช้ DB)
│   ├── langmind_client.py # เชื่อมต่อ LangMind API (STT / Agent / TTS)
│   ├── audio_converter.py # แปลงเสียง Opus / PCM / MP3 ด้วย FFmpeg
│   └── screen_image.py    # สร้างภาพ PNG สถานะสำหรับหน้าจอบอร์ด
├── admin-ui/              # Frontend Admin Dashboard (React + TypeScript + Vite)
├── docs/                  # เอกสารเพิ่มเติม (เช่น BOARD2.md)
├── scripts/               # สคริปต์ช่วยพัฒนาและทดสอบ (mock_device.py, inject_ota.py, parse_nvs.py)
├── tests/                 # ชุดทดสอบอัตโนมัติ (pytest)
├── config.example.yaml    # ตัวอย่างไฟล์ตั้งค่าสำหรับรันบนเครื่อง
├── .env.example           # ตัวอย่างตัวแปรสภาพแวดล้อมสำหรับ Docker Compose
├── Dockerfile             # Multi-stage build (Node 22 + Python 3.12 + FFmpeg)
└── docker-compose.yml     # รัน Server คู่กับ PostgreSQL 16
```

## เริ่มต้นใช้งานด้วย Docker Compose (แนะนำ)

1. คัดลอกไฟล์ตัวอย่าง environment และกรอกค่าที่จำเป็น:

```bash
cp .env.example .env
```

แก้ไข `.env` โดยตั้งค่าอย่างน้อย:
- `LANGMIND_API_KEY`
- `LANGMIND_AGENT_ID`
- `ADMIN_PASSWORD`

2. สร้างและรันคอนเทนเนอร์ (Server + PostgreSQL):

```bash
docker compose up -d --build
```

3. ตรวจสอบสถานะเซิร์ฟเวอร์และเปิด Admin Dashboard:
- Health check: `http://localhost:8000/healthz`
- Admin Dashboard: `http://localhost:8000/admin`

## การรันบนเครื่อง (Local Development)

### 1. ติดตั้ง Dependencies

ต้องมี **Python 3.12+**, **FFmpeg** และ **Node.js 22+** (สำหรับบิลด์ Admin UI)

```bash
pip install -r requirements.txt
```

หากต้องการใช้งาน Admin Dashboard บนเครื่อง ให้บิลด์ `admin-ui`:

```bash
cd admin-ui
npm ci
npm run build
cd ..
```

(หรือรัน PostgreSQL เฉพาะตัวฐานข้อมูลด้วย `docker compose up -d postgres`)

### 2. ตั้งค่า Config / Environment

คัดลอก [`config.example.yaml`](config.example.yaml) เป็น `config.yaml` สำหรับค่า non-secret บนเครื่อง (ไฟล์ `config.yaml` ถูก ignore โดย Git) และตั้งค่า secret ผ่าน environment variables:

```powershell
$env:LANGMIND_API_KEY = "<your-langmind-api-key>"
$env:LANGMIND_AGENT_ID = "<your-agent-id>"
$env:ADMIN_PASSWORD = "<your-admin-password>"
$env:DATABASE_URL = "postgresql://xiaozhi:xiaozhi_local_dev@127.0.0.1:5432/xiaozhi"
uvicorn app.server:app --host 0.0.0.0 --port 8000 --reload
```

> หากไม่ได้ตั้ง `DATABASE_URL` เซิร์ฟเวอร์จะทำงานในโหมด YAML/Env โดยใช้ `DEVICE_TOKEN` (สำหรับ `board1`) หรือ `DEVICE_TOKENS="board2:<token>,..."` แทน

## Environment Variables

| Variable | คำอธิบาย | ค่าเริ่มต้น |
|---|---|---|
| `APP_ENV` | โหมดการทำงาน (`development` / `production`) | `development` |
| `PORT` | พอร์ตของเซิร์ฟเวอร์ | `8000` |
| `PUBLIC_URL` | WebSocket URL สาธารณะ เช่น `wss://<domain>/ws` (บังคับใน `production`) | `""` |
| `DATABASE_URL` | PostgreSQL connection string สำหรับเก็บข้อมูลบอร์ดและโปรไฟล์ | `""` |
| `ADMIN_PASSWORD` | รหัสผ่านสำหรับเข้าสู่ระบบ `/admin` | `""` |
| `LANGMIND_BASE_URL` | Base URL ของ LangMind API | `https://langmind.in.th/api` |
| `LANGMIND_API_KEY` | API key ของ LangMind (ต้องเก็บเป็น secret) | `""` |
| `LANGMIND_AGENT_ID` | Agent ID ของ LangMind ที่ต้องการใช้งาน | `""` |
| `VOICE_SPEED` | ความเร็วเสียงพูด TTS | `1.0` |
| `DEVICE_TOKEN` / `DEVICE_TOKENS` | โทเค็นบอร์ด (ใช้เมื่อไม่ได้เปิด PostgreSQL) | `""` |
| `MAX_FFMPEG_WORKERS` | จำนวน FFmpeg worker สูงสุดพร้อมกัน | `6` |
| `LOG_TRANSCRIPT` | บันทึกข้อความสนทนาลง log (`true` / `false`) | `false` |
| `TIMEZONE` / `TIMEZONE_OFFSET_MIN` | เขตเวลาที่ส่งให้บอร์ดผ่าน `/ota` | `Asia/Bangkok` / `420` |

> **ความปลอดภัย**: ห้าม commit API key, `ADMIN_PASSWORD`, `DEVICE_TOKEN`, `.env` หรือ `config.yaml` ลงใน Git

## Deploy บน Cloud (เช่น Render / Docker Host)

- **Docker**: ใช้ `Dockerfile` ในโปรเจกต์ได้ทันที (รวมขั้นตอน build `admin-ui`, ติดตั้ง `ffmpeg` และฟอนต์ไทย `fonts-thai-tlwg` แล้ว)
- **Python Web Service (Render)**:
  - Build command: `pip install -r requirements.txt` (และบิลด์ `admin-ui` หากต้องการใช้ `/admin`)
  - Start command: `uvicorn app.server:app --host 0.0.0.0 --port $PORT`
  - Health check path: `/healthz`
  - ตั้ง `APP_ENV=production`, `PUBLIC_URL=wss://<your-service>/ws`, `DATABASE_URL`, `ADMIN_PASSWORD`, `LANGMIND_API_KEY`, `LANGMIND_AGENT_ID`

## Endpoints หลัก

| Method / Proto | Path | คำอธิบาย |
|---|---|---|
| `GET` | `/healthz` | ตรวจสอบความพร้อมของเซิร์ฟเวอร์ (คืน `503` หากตั้งค่าไม่ครบ) |
| `GET` / `POST` | `/ota` | บอร์ดเรียกตอนบูตเพื่อรับ `websocket.url`, `token` และ `server_time` |
| `WS` | `/ws` | ช่องทางสื่อสารเสียงและโปรโตคอล XiaoZhi (ตรวจ `Authorization: Bearer <token>`) |
| `GET` | `/admin` | หน้า Admin Dashboard (SPA) |
| `REST` | `/api/admin/*` | API สำหรับจัดการอุปกรณ์และโปรไฟล์ (ต้องล็อกอิน) |
| `GET` | `/screen/{profile}/{slot}.png` | ภาพสถานะ PNG สำหรับแสดงบนหน้าจอบอร์ด |
| `POST` | `/screen/upload` | รับภาพ snapshot จากบอร์ด |

## การทดสอบ (Tests)

```bash
pytest -v
```

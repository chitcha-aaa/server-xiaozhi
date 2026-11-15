# XiaoZhi ESP32 ↔ LangMind Bridge

เซิร์ฟเวอร์รับเสียงจาก XiaoZhi ผ่าน WebSocket ที่ยืนยันตัวอุปกรณ์ แล้วเรียก LangMind ตาม agent ที่กำหนดไว้ จากนั้นส่งเสียงตอบกลับผ่าน connection เดิม บอร์ดเรียก `/ota` เพื่อรับ WebSocket URL เท่านั้น; response นี้ไม่มี credential

## Deploy บน Render

สร้าง **Web Service** จาก repository นี้ โดยตั้งค่า:

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn server:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/healthz` |
| Instance | Free สำหรับ prototype/staging |

ตั้ง Environment Variables ใน Render dashboard:

| Variable | ค่า |
|---|---|
| `APP_ENV` | `production` |
| `PUBLIC_URL` | `wss://<ชื่อ-service>.onrender.com/ws` |
| `LANGMIND_BASE_URL` | `https://langmind.in.th/api` |
| `LANGMIND_API_KEY` | LangMind key ที่หมุนใหม่แล้ว; ตั้งเป็น secret ใน Render |
| `LANGMIND_AGENT_ID` | Agent ID ที่ต้องการล็อกใช้ |
| `DEVICE_TOKEN` | token แบบสุ่มอย่างน้อย 32 ตัวอักษรเฉพาะบอร์ด; ตั้งเป็น secret ใน Render และ provision ค่าเดียวกันให้บอร์ดแยกต่างหาก |
| `VOICE_SPEED` | ไม่บังคับ; ค่าเริ่มต้น `1.0` |

อย่าใส่ API key หรือ device token ใน Git, `config.yaml`, log, URL หรือ `/ota` response. API key ที่เคยเผยแพร่ต้อง revoke ก่อน deploy. `config.yaml` ใช้เฉพาะ local non-secret settings และถูก ignore โดย Git; ตัวอย่างอยู่ใน [`config.example.yaml`](config.example.yaml).

## Board setup

ตั้ง OTA URL ของบอร์ดเป็น `https://<ชื่อ-service>.onrender.com/ota`. `/ota` จะตอบด้วย URL `wss://.../ws` ที่กำหนดผ่าน `PUBLIC_URL`. ห้ามเปลี่ยน host ด้วย request `Host` header.

`/ws` ตรวจ `Authorization: Bearer <DEVICE_TOKEN>` ก่อน accept WebSocket. Firmware ใน repo อ่าน token จาก WebSocket setting ใน NVS; ต้อง provision token ให้ตรงกับ Render แยกจากการตั้ง OTA URL. การแก้ server นี้ไม่เขียน token ลงบอร์ดหรือเปลี่ยน firmware.

เมื่อบอร์ดเริ่มฟัง จะส่ง Opus frames ระหว่างที่ปุ่มเปิดฟังอยู่เท่านั้น; server ใช้ VAD ตรวจจบเสียง, เรียก STT → agent → TTS และรองรับปุ่มหยุด/ตัดคำตอบตาม protocol. ไม่มี wake word/always-on mic ใน scope นี้. `/healthz` เป็น readiness endpoint; ถ้า credential หรือ WSS URL ขาด จะตอบ `503` และ `/ota` จะไม่ส่งค่าตั้งต้นที่ไม่ปลอดภัย.

Render Free อาจ sleep หลังไม่มี traffic และมี cold start; ใช้สำหรับ prototype/staging.

## Local run

ติดตั้ง FFmpeg และ Python dependencies จาก `requirements.txt`, ตั้ง `LANGMIND_API_KEY`, `LANGMIND_AGENT_ID`, `DEVICE_TOKEN` ใน environment แล้วรัน:

```powershell
$env:LANGMIND_API_KEY = "<rotated-key>"
$env:LANGMIND_AGENT_ID = "<agent-id>"
$env:DEVICE_TOKEN = "<device-token>"
python server.py
```

คำสั่งตัวอย่างนี้ตั้งค่าตัวแปรเฉพาะ terminal ปัจจุบัน; อย่า commit ค่าแทน placeholder ลงไฟล์.

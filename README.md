# 🚀 XiaoZhi ESP32 <-> LangMind Bridge Server

เซิร์ฟเวอร์ตัวกลางสำหรับเชื่อมต่อระหว่าง **หุ่นยนต์ XiaoZhi ESP32** กับ **LangMind AI Platform (NECTEC)** โดยประมวลผลผ่าน LangMind ทั้งหมด 100%:
* 🎙️ **STT (ถอดเสียงพูด):** ผ่าน `POST /v1/agents/{agent_id}/voice/transcribe`
* 🧠 **LLM (สมองคิด):** ผ่าน `POST /chat/message/blocking`
* 🔊 **TTS (สร้างเสียงพูด):** ผ่าน `POST /v1/agents/{agent_id}/voice/speech`

---

## 🛠️ ขั้นตอนการใช้งาน

### 1. ใส่ API Key และ Agent ID
เปิดไฟล์ `config.yaml` แล้วแก้ไข 2 บรรทัดนี้:
```yaml
langmind:
  api_key: "ใส่_Personal_API_Key_จาก_LangMind_ที่นี่"
  agent_id: "ใส่_Agent_ID_จาก_LangMind_ที่นี่"
```

### 2. รันเซิร์ฟเวอร์
ดับเบิลคลิกไฟล์ `run.bat` หรือรันผ่าน Terminal:
```cmd
cd D:\xiaozhi\server
python server.py
```

เมื่อเซิร์ฟเวอร์เริ่มทำงาน หน้าจอจะแสดงข้อความประมาณนี้:
```
=================================================================
  🚀 XiaoZhi <-> LangMind Bridge Server is running!
=================================================================
  • Local IP:         192.168.1.50
  • Server Port:      8000
  • OTA URL for ESP32: http://192.168.1.50:8000/ota/
  • WebSocket URL:    ws://192.168.1.50:8000/ws
  • LangMind Agent:   usr_agent_xxxx
=================================================================
```

---

### 3. ตั้งค่าบอร์ด ESP32 ให้ชี้มาที่เซิร์ฟเวอร์ (ไม่ต้องแก้โค้ดบอร์ด)

1. **เข้าโหมด Wi-Fi Config บนบอร์ด:**
   * กดปุ่ม **Boot** บนบอร์ด XiaoZhi ค้างไว้ขณะเปิดเครื่อง (หรือกด Reset)
   * บอร์ดจะปล่อย Wi-Fi ชื่อ `Xiaozhi-XXXX`
2. **เชื่อมต่อและตั้งค่า:**
   * ใช้มือถือหรือคอมพิวเตอร์เชื่อมต่อ Wi-Fi `Xiaozhi-XXXX`
   * เปิดเบราว์เซอร์ไปที่ `http://192.168.4.1`
   * ในช่อง **OTA URL** ให้ใส่ URL ที่แสดงในหน้าจอเซิร์ฟเวอร์ เช่น:
     ```
     http://192.168.1.50:8000/ota/
     ```
3. **บันทึก:**
   * กด **Save / Submit** บอร์ดจะรีสตาร์ทและเชื่อมต่อเข้ามาที่เซิร์ฟเวอร์ทันที

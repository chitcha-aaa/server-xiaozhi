import sys
import struct

filename = sys.argv[1] if len(sys.argv) > 1 else 'nvs_dump.bin'
with open(filename, 'rb') as f:
    data = f.read()

for p in range(len(data) // 4096):
    page = data[p*4096 : (p+1)*4096]
    state, seq = struct.unpack('<II', page[:8])
    if state == 0xFFFFFFFF:
        continue
    print(f'=== Page {p}: State=0x{state:08x}, Seq={seq} ===')
    bitmap = page[32:64]
    
    def get_slot_state(slot):
        byte_idx = (slot + 2) // 4
        bit_offset = ((slot + 2) % 4) * 2
        return (bitmap[byte_idx] >> bit_offset) & 0x03

    ns_map = {}
    slot = 0
    while slot < 126:
        st = get_slot_state(slot)
        if st == 0b10: # written / valid
            entry_offset = 64 + slot * 32
            entry = page[entry_offset:entry_offset+32]
            ns, typ, span, chunk, crc = struct.unpack('<BBBB I', entry[:8])
            key = entry[8:24].split(b'\x00')[0].decode('latin1', errors='ignore')
            val_bytes = entry[24:32]
            if ns == 0 and typ == 0x01:
                ns_idx = val_bytes[0]
                ns_map[ns_idx] = key
                print(f'  Slot {slot:2d}: Namespace definition: "{key}" -> index {ns_idx}')
            else:
                ns_name = ns_map.get(ns, f'ns_{ns}')
                if typ == 0x21: # string
                    str_len, str_crc = struct.unpack('<HH', val_bytes[:4])
                    # String data spans across next (span-1) slots
                    str_data_raw = page[entry_offset+32 : entry_offset + span*32]
                    str_val = str_data_raw[:str_len].split(b'\x00')[0].decode('latin1', errors='ignore')
                    print(f'  Slot {slot:2d} (span {span}): [{ns_name}] string key="{key}" = "{str_val}"')
                elif typ in (0x01, 0x02, 0x04, 0x08, 0x11, 0x12):
                    print(f'  Slot {slot:2d}: [{ns_name}] int key="{key}" = {val_bytes[:4].hex()}')
                else:
                    print(f'  Slot {slot:2d} (span {span}): [{ns_name}] type=0x{typ:02x} key="{key}"')
            slot += max(1, span)
        else:
            slot += 1

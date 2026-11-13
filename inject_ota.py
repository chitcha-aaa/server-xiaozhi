import struct
import zlib

def update_ota(input_bin, output_bin, new_url):
    with open(input_bin, 'rb') as f:
        data = bytearray(f.read())
        
    # 1. In Page 1, mark old slot 71 and 72 as erased (0b00)
    page1_offset = 4096
    for s in [71, 72]:
        byte_idx = page1_offset + 32 + (s + 2) // 4
        bit_offset = ((s + 2) % 4) * 2
        data[byte_idx] &= ~(0x03 << bit_offset)
        
    # 2. In Page 3 (active page), inject new_url at slot 57
    page3_offset = 3 * 4096
    start_slot = 57
    val_data = new_url.encode('utf-8') + b'\x00'
    str_len = len(val_data)
    data_slots = (str_len + 31) // 32
    span = 1 + data_slots
    
    print(f"Injecting into Page 3 slot {start_slot}..{start_slot+span-1}: '{new_url}' (len={str_len}, span={span})")
    
    # Update bitmap in Page 3
    for s in range(start_slot, start_slot + span):
        byte_idx = page3_offset + 32 + (s + 2) // 4
        bit_offset = ((s + 2) % 4) * 2
        data[byte_idx] &= ~(0x03 << bit_offset)
        data[byte_idx] |= (0x02 << bit_offset)
        
    # Header entry
    ns_idx = 2 # wifi
    datatype = 0x21 # string
    chunk_idx = 0xFF
    key_bytes = b'ota_url'.ljust(16, b'\x00')
    
    data_crc = zlib.crc32(val_data, 0xFFFFFFFF) & 0xFFFFFFFF
    data_field = struct.pack('<HHI', str_len, 0xFFFF, data_crc)
    
    entry_crc_data = bytes([ns_idx, datatype, span, chunk_idx]) + key_bytes + data_field
    header_crc = zlib.crc32(entry_crc_data, 0xFFFFFFFF) & 0xFFFFFFFF
    
    header_entry = (
        bytes([ns_idx, datatype, span, chunk_idx]) +
        struct.pack('<I', header_crc) +
        key_bytes +
        data_field
    )
    
    entry_offset = page3_offset + 64 + start_slot * 32
    data[entry_offset : entry_offset + 32] = header_entry
    
    padded_val = val_data.ljust(data_slots * 32, b'\xff')
    for d in range(data_slots):
        d_slot = start_slot + 1 + d
        d_offset = page3_offset + 64 + d_slot * 32
        data[d_offset : d_offset + 32] = padded_val[d*32 : (d+1)*32]
        
    with open(output_bin, 'wb') as f_out:
        f_out.write(data)
    print(f"Generated {output_bin} successfully!")

update_ota('nvs_dump.bin', 'nvs_modified.bin', 'http://10.221.66.211:8000/ota/')

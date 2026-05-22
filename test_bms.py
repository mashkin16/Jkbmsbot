import asyncio
from bleak import BleakClient

ADDRESS = 'C8:47:80:42:25:AF'
UUID = '0000ffe1-0000-1000-8000-00805f9b34fb'
CMD_DEV  = bytes([0xAA,0x55,0x90,0xEB,0x97,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0x11])
CMD_CELL = bytes([0xAA,0x55,0x90,0xEB,0x96,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0x10])

async def main():
    async with BleakClient(ADDRESS) as client:
        print("Підключено!")

        for attempt in range(3):
            buf = []
            def h(_, d): buf.extend(d)
            await client.start_notify(UUID, h)
            await asyncio.sleep(0.2)
            buf.clear()

            print(f"\nСпроба {attempt+1}: надсилаємо Device Info (0x97)...")
            await client.write_gatt_char(UUID, CMD_DEV, response=True)
            await asyncio.sleep(1.0)
            ftype1 = f"0x{buf[4]:02X}" if len(buf) > 4 else "??"
            print(f"  Отримано: {len(buf)} байт, тип: {ftype1}")
            buf.clear()

            print(f"Спроба {attempt+1}: надсилаємо Cell Info (0x96)...")
            await client.write_gatt_char(UUID, CMD_CELL, response=True)
            await asyncio.sleep(2.0)

            try:
                await client.stop_notify(UUID)
            except Exception:
                pass

            if len(buf) > 4:
                ftype = buf[4]
                print(f"  Отримано: {len(buf)} байт, тип: 0x{ftype:02X}")
                if ftype == 0x02:
                    print("  Cell Data! Ячейки:")
                    for i in range(8):
                        off = 6 + i*4
                        if off+1 < len(buf):
                            v = (buf[off+1] << 8) | buf[off]
                            if v > 500:
                                print(f"    C{i+1} = {v/1000:.3f}V")
                    break
                elif ftype == 0x03:
                    print("  Device Info знову — чекаємо довше...")
                    await asyncio.sleep(2.0)
            else:
                print("  Даних немає")

            await asyncio.sleep(1.0)

asyncio.run(main())

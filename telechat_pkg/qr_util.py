"""Standalone QR code generator for terminal display. No heavy dependencies."""
from __future__ import annotations

import socket


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def print_web_qr(port: str) -> None:
    ip = _get_local_ip()
    url = f"http://{ip}:{port}"

    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(box_size=1, border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
    except ImportError:
        matrix = _qr_encode_minimal(url)
        if not matrix:
            print(f"\n  Open on your phone: {url}")
            return

    print(f"\n  ── Scan to open on your phone ──\n")
    _render_qr_terminal(matrix)
    print(f"\n  {url}")


def _render_qr_terminal(matrix: list[list[bool]]) -> None:
    rows = len(matrix)
    for y in range(0, rows, 2):
        line = "  "
        for x in range(len(matrix[0])):
            top = matrix[y][x]
            bot = matrix[y + 1][x] if y + 1 < rows else False
            if top and bot:
                line += "█"
            elif top and not bot:
                line += "▀"
            elif not top and bot:
                line += "▄"
            else:
                line += " "
        print(line)


def _qr_encode_minimal(data: str) -> list[list[bool]] | None:
    bdata = data.encode("utf-8")
    length = len(bdata)

    versions = [
        (1, 26, 7, 17),
        (2, 44, 10, 32),
        (3, 70, 15, 53),
        (4, 100, 20, 78),
    ]

    ver_info = None
    for v in versions:
        if length <= v[3]:
            ver_info = v
            break
    if not ver_info:
        return None

    version, total_cw, ec_cw, _ = ver_info
    data_cw = total_cw - ec_cw
    size = 17 + version * 4

    bits = "0100"
    if version <= 1:
        bits += format(length, "08b")
    else:
        bits += format(length, "016b")

    for b in bdata:
        bits += format(b, "08b")

    bits += "0000"
    while len(bits) % 8 != 0:
        bits += "0"

    codewords = []
    for i in range(0, len(bits), 8):
        if i + 8 <= len(bits):
            codewords.append(int(bits[i:i + 8], 2))

    pad_bytes = [0xEC, 0x11]
    pi = 0
    while len(codewords) < data_cw:
        codewords.append(pad_bytes[pi % 2])
        pi += 1

    ec = _rs_encode(codewords[:data_cw], ec_cw)
    final = codewords[:data_cw] + ec

    matrix = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def _set(r, c, val, reserve=True):
        if 0 <= r < size and 0 <= c < size:
            matrix[r][c] = val
            if reserve:
                reserved[r][c] = True

    def _finder(row, col):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if 0 <= rr < size and 0 <= cc < size:
                    if 0 <= r <= 6 and 0 <= c <= 6:
                        if r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4):
                            _set(rr, cc, True)
                        else:
                            _set(rr, cc, False)
                    else:
                        _set(rr, cc, False)

    _finder(0, 0)
    _finder(0, size - 7)
    _finder(size - 7, 0)

    for i in range(8, size - 8):
        v = i % 2 == 0
        _set(6, i, v)
        _set(i, 6, v)

    _set(size - 8, 8, True)

    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    if version >= 2:
        apos = {2: [6, 18], 3: [6, 22], 4: [6, 26]}
        positions = apos.get(version, [])
        for ar in positions:
            for ac in positions:
                if reserved[ar][ac]:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        v = abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0)
                        _set(ar + dr, ac + dc, v)

    all_bits = ""
    for byte in final:
        all_bits += format(byte, "08b")

    bit_idx = 0
    col = size - 1
    going_up = True

    while col >= 0:
        if col == 6:
            col -= 1
            continue
        rows_range = range(size - 1, -1, -1) if going_up else range(size)
        for row in rows_range:
            for dc in [0, -1]:
                c = col + dc
                if 0 <= c < size and not reserved[row][c]:
                    if bit_idx < len(all_bits):
                        matrix[row][c] = all_bits[bit_idx] == "1"
                    bit_idx += 1
        col -= 2
        going_up = not going_up

    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and (r + c) % 2 == 0:
                matrix[r][c] = not matrix[r][c]

    fmt_bits = "111011111000100"
    fmt_positions_h = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5),
                       (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8),
                       (2, 8), (1, 8), (0, 8)]
    fmt_positions_v = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
                       (size - 5, 8), (size - 6, 8), (size - 7, 8),
                       (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
                       (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]

    for i, bit in enumerate(fmt_bits):
        v = bit == "1"
        r, c = fmt_positions_h[i]
        matrix[r][c] = v
        r, c = fmt_positions_v[i]
        matrix[r][c] = v

    return matrix


def _rs_encode(data: list[int], nsym: int) -> list[int]:
    gf_exp = [0] * 512
    gf_log = [0] * 256
    x = 1
    for i in range(255):
        gf_exp[i] = x
        gf_log[x] = i
        x <<= 1
        if x & 256:
            x ^= 0x11d
    for i in range(255, 512):
        gf_exp[i] = gf_exp[i - 255]

    def gf_mul(a, b):
        if a == 0 or b == 0:
            return 0
        return gf_exp[gf_log[a] + gf_log[b]]

    gen = [1]
    for i in range(nsym):
        new_gen = [0] * (len(gen) + 1)
        for j, coeff in enumerate(gen):
            new_gen[j] ^= coeff
            new_gen[j + 1] ^= gf_mul(coeff, gf_exp[i])
        gen = new_gen

    feedback = [0] * (len(data) + nsym)
    feedback[:len(data)] = data[:]
    for i in range(len(data)):
        if feedback[i] != 0:
            for j in range(1, len(gen)):
                feedback[i + j] ^= gf_mul(gen[j], feedback[i])
    return feedback[len(data):]

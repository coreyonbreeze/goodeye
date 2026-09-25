"""Minimal QR code encoder (byte mode, error correction L or M, versions 1-10). Standard library only.

    matrix = qr_matrix("http://192.168.1.20:4400/?t=abc")   # list of rows of bools, no quiet zone
    print(to_terminal(matrix)); svg = to_svg(matrix)
"""

# (EC codewords per block, blocks in group 1, data codewords per block, blocks in group 2, data codewords per block)
EC_TABLE = {
    "L": {1: (7, 1, 19, 0, 0), 2: (10, 1, 34, 0, 0), 3: (15, 1, 55, 0, 0), 4: (20, 1, 80, 0, 0), 5: (26, 1, 108, 0, 0),
          6: (18, 2, 68, 0, 0), 7: (20, 2, 78, 0, 0), 8: (24, 2, 97, 0, 0), 9: (30, 2, 116, 0, 0), 10: (18, 2, 68, 2, 69)},
    "M": {1: (10, 1, 16, 0, 0), 2: (16, 1, 28, 0, 0), 3: (26, 1, 44, 0, 0), 4: (18, 2, 32, 0, 0), 5: (24, 2, 43, 0, 0),
          6: (16, 4, 27, 0, 0), 7: (18, 4, 31, 0, 0), 8: (22, 2, 38, 2, 39), 9: (22, 3, 36, 2, 37), 10: (26, 4, 43, 1, 44)},
}
EC_BITS = {"L": 1, "M": 0}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50]}

# GF(256) with the QR polynomial 0x11D
EXP, LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _rs_ec(data, n):
    gen = [1]
    for i in range(n):
        gen = [a ^ b for a, b in zip(gen + [0], [0] + [_gf_mul(g, EXP[i]) for g in gen])]
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gf_mul(gen[j], c)
    return rem[len(data):]


def _bch(value, poly, bits):
    v = value << (poly.bit_length() - 1)
    while v.bit_length() >= poly.bit_length():
        v ^= poly << (v.bit_length() - poly.bit_length())
    return (value << (poly.bit_length() - 1)) | v


def _codewords(data, version, level):
    ec_n, b1, d1, b2, d2 = EC_TABLE[level][version]
    capacity = b1 * d1 + b2 * d2
    bits = [0, 1, 0, 0]                                     # byte mode
    count_bits = 8 if version < 10 else 16
    bits += [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0
    while len(words) < capacity:
        words.append((0xEC, 0x11)[pad % 2])
        pad += 1
    blocks, pos = [], 0
    for count, size in ((b1, d1), (b2, d2)):
        for _ in range(count):
            blocks.append(words[pos:pos + size])
            pos += size
    ecs = [_rs_ec(b, ec_n) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_n):
        out += [e[i] for e in ecs]
    return out


def _base(version):
    size = 17 + 4 * version
    m = [[None] * size for _ in range(size)]

    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    m[rr][cc] = (0 <= dr <= 6 and dc in (0, 6)) or (0 <= dc <= 6 and dr in (0, 6)) or (2 <= dr <= 4 and 2 <= dc <= 4)

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)
    for i in range(8, size - 8):
        m[6][i] = m[i][6] = i % 2 == 0
    pos = ALIGN[version]
    for r in pos:
        for c in pos:
            if (r, c) in ((pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])):
                continue                                    # these overlap the finder patterns
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
    for i in range(9):                                      # reserve format areas
        for r, c in ((8, i), (i, 8)):
            if m[r][c] is None:
                m[r][c] = False
    for i in range(8):
        if m[8][size - 1 - i] is None:
            m[8][size - 1 - i] = False
        if m[size - 1 - i][8] is None:
            m[size - 1 - i][8] = False
    m[size - 8][8] = True                                   # dark module
    if version >= 7:
        v = _bch(version, 0x1F25, 18)
        for i in range(18):
            bit = bool((v >> i) & 1)
            m[size - 11 + i % 3][i // 3] = bit
            m[i // 3][size - 11 + i % 3] = bit
    return m


MASKS = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
         lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
         lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]


def _place(base, words, mask, level):
    size = len(base)
    m = [row[:] for row in base]
    func = [[cell is not None for cell in row] for row in base]
    bits = [(w >> i) & 1 for w in words for i in range(7, -1, -1)]
    k, col, up = 0, size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not func[r][c]:
                    bit = bits[k] if k < len(bits) else 0
                    k += 1
                    m[r][c] = bool(bit) != MASKS[mask](r, c)
        col -= 2
        up = not up
    fmt = _bch((EC_BITS[level] << 3) | mask, 0x537, 15) ^ 0x5412
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        if i < 6:
            m[i][8] = bit
        elif i < 8:
            m[i + 1][8] = bit
        elif i == 8:
            m[8][7] = bit                                   # skip the timing column
        else:
            m[8][14 - i] = bit
        if i < 8:
            m[8][size - 1 - i] = bit
        else:
            m[size - 15 + i][8] = bit
    m[size - 8][8] = True
    return m


def _penalty(m):
    size, score = len(m), 0
    for grid in (m, [list(col) for col in zip(*m)]):
        for row in grid:
            run = 1
            for i in range(1, size):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
            score += run - 2 if run >= 5 else 0
            s = "".join("1" if x else "0" for x in row)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(row) for row in m) * 100 / (size * size)
    return score + int(abs(dark - 50) // 5) * 10


def qr_matrix(text, level="M"):
    data = text.encode("utf-8")
    for version in range(1, 11):
        ec_n, b1, d1, b2, d2 = EC_TABLE[level][version]
        header = 4 + (8 if version < 10 else 16)
        if header + 8 * len(data) <= 8 * (b1 * d1 + b2 * d2):
            break
    else:
        raise ValueError("text too long for a version 10 QR code")
    words, base = _codewords(data, version, level), _base(version)
    return min((_place(base, words, mask, level) for mask in range(8)), key=_penalty)


def to_terminal(m, border=2):
    """Two rows per line with half blocks. Light modules print as block characters (works on dark terminals)."""
    size = len(m)
    get = lambda r, c: 0 <= r < size and 0 <= c < size and m[r][c]
    lines = []
    for r in range(-border, size + border, 2):
        line = ""
        for c in range(-border, size + border):
            top, bottom = not get(r, c), not get(r + 1, c)
            line += "█" if top and bottom else "▀" if top else "▄" if bottom else " "
        lines.append(line)
    return "\n".join(lines)


def to_svg(m, border=4, scale=6):
    size = len(m) + 2 * border
    path = "".join(f"M{c + border},{r + border}h1v1h-1z" for r, row in enumerate(m) for c, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size * scale}" height="{size * scale}" '
            f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/><path d="{path}" fill="#000"/></svg>')

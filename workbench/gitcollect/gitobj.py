"""Git 对象哈希、树/提交对象构造解析与 LFS pointer 识别。"""
import hashlib


def object_id(type_name, content):
    """按 Git 规则计算对象 ID：sha1("<type> <len>\\0" + content)。"""
    header = f'{type_name} {len(content)}\x00'.encode('ascii')
    return hashlib.sha1(header + content).hexdigest()


def tree_sort_key(name, mode):
    """Git 树排序：子树条目按 name+'/' 参与比较，其余按 name。"""
    if mode == '040000':
        return name + '/'
    return name


def build_tree(entries):
    """entries 为 (mode, oid, name) 列表，返回树对象原始字节。

    Git 树对象中子树模式规范写法为 40000（ls-tree 显示为 040000），
    其余模式保持六位写法。
    """
    parts = []
    for mode, oid, name in sorted(entries, key=lambda e: tree_sort_key(e[2], e[0])):
        wire_mode = '40000' if mode == '040000' else mode
        parts.append(f'{wire_mode} {name}\x00'.encode('utf-8'))
        parts.append(bytes.fromhex(oid))
    return b''.join(parts)


def parse_tree(content):
    """解析树对象字节，生成 (mode, oid, name) 列表。"""
    out = []
    i = 0
    while i < len(content):
        nul = content.index(b'\x00', i)
        head = content[i:nul].decode('utf-8')
        mode, name = head.split(' ', 1)
        if mode == '40000':
            mode = '040000'
        oid = content[nul + 1:nul + 21].hex()
        out.append((mode, oid, name))
        i = nul + 21
    return out


def parse_commit(content):
    """解析提交对象头部，返回 dict（tree/parents/author 等）。"""
    head, _, message = content.partition(b'\n\n')
    info = {'parents': [], 'message': message}
    for line in head.split(b'\n'):
        key, _, value = line.partition(b' ')
        if key == b'tree':
            info['tree'] = value.decode('ascii')
        elif key == b'parent':
            info['parents'].append(value.decode('ascii'))
        elif key in (b'author', b'committer'):
            info[key.decode('ascii')] = value.decode('utf-8', 'replace')
    return info


def is_lfs_pointer(data):
    """识别 Git LFS pointer 文本（只识别，不下载 LFS 对象）。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) > 1024:
        return False
    if not data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
        return False
    fields = {}
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return False
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines = lines[:-1]
    for line in lines[1:]:
        key, sep, value = line.partition(' ')
        if not sep:
            return False
        fields[key] = value
    if not fields.get('oid', '').startswith('sha256:'):
        return False
    if 'size' not in fields or not fields['size'].isdigit():
        return False
    return True

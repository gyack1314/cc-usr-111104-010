"""命令行入口：资料检查、固定提交收集、离线核对与合成演示。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from .gitcollect import CollectionError, Config, collect, verify_package
from .gitcollect.collector import MANIFEST_NAME


def _cmd_inspect(a):
    root = Path(a.directory)
    if not root.is_dir():
        raise SystemExit('资料目录不存在')
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_file():
            data = path.read_bytes()
            rows.append({'path': path.relative_to(root).as_posix(), 'size': len(data),
                         'sha256': hashlib.sha256(data).hexdigest()})
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def _load_collect_config(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or 'whitelist' not in value:
        raise CollectionError('收集配置必须包含 whitelist 数组')
    return value, Config(
        whitelist=list(value['whitelist']),
        max_depth=int(value.get('max_depth', 8)),
        max_paths=int(value.get('max_paths', 10000)),
        max_bytes=int(value.get('max_bytes', 100 * 1024 * 1024)),
        work_dir=value.get('work_dir'),
    )


def _cmd_collect(a):
    _value, config = _load_collect_config(a.config)
    entries = json.loads(Path(a.entries).read_text(encoding='utf-8'))
    if not isinstance(entries, list) or not entries:
        raise CollectionError('入口文件必须是非空数组，每项含 location 与 ref')
    if len(entries) > 1:
        raise CollectionError('单次采集只接受一个入口；多入口请分别输出到不同资料包')
    item = entries[0]
    manifest = collect(item['location'], item['ref'], config, a.output)
    print(json.dumps({'status': 'ok', 'output': str(Path(a.output) / 'package'),
                      'root_commit': manifest['root']['commit'],
                      'export_paths': manifest['counts']['export_paths'],
                      'content_bytes': manifest['counts']['content_bytes']},
                     ensure_ascii=False, indent=2))


def _cmd_verify(a):
    report = verify_package(a.package)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _cmd_demo_repos(a):
    from .gitcollect.demo import build_demo_repos
    result = build_demo_repos(a.directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _cmd_demo_run(a):
    from .gitcollect.demo import run_demo
    result = run_demo(a.repos, a.output, base_url=a.base_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _cmd_http_serve(a):
    from .gitcollect.demo_http import serve
    info = serve(a.repo, host=a.host, port=a.port)
    print(json.dumps(info, ensure_ascii=False), flush=True)
    import threading
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog='workbench.cli')
    sub = p.add_subparsers(dest='command', required=True)

    sp = sub.add_parser('inspect', help='查看本地资料文件及其字节指纹')
    sp.add_argument('directory'); sp.set_defaults(func=_cmd_inspect)

    sp = sub.add_parser('collect', help='按固定提交递归收集 Git 源码资料包')
    sp.add_argument('config', help='含 whitelist 与预算的 JSON 配置')
    sp.add_argument('entries', help='入口数组 JSON（location/ref）')
    sp.add_argument('output', help='输出目录（资料包写入其 package/ 子目录）')
    sp.set_defaults(func=_cmd_collect)

    sp = sub.add_parser('verify', help='离线核对资料包')
    sp.add_argument('package')
    sp.set_defaults(func=_cmd_verify)

    sp = sub.add_parser('demo-repos', help='生成覆盖全部演示场景的本地合成 Git 仓库')
    sp.add_argument('directory')
    sp.set_defaults(func=_cmd_demo_repos)

    sp = sub.add_parser('demo-run', help='运行全部演示场景并自检')
    sp.add_argument('repos')
    sp.add_argument('output')
    sp.add_argument('--base-url', default=None, help='可选 HTTP 智能协议基址')
    sp.set_defaults(func=_cmd_demo_run)

    sp = sub.add_parser('http-serve', help='把本地裸库经环回 HTTP 智能协议暴露（仅合成演示）')
    sp.add_argument('repo')
    sp.add_argument('--host', default='127.0.0.1')
    sp.add_argument('--port', type=int, default=0)
    sp.set_defaults(func=_cmd_http_serve)

    a = p.parse_args(argv)
    try:
        a.func(a)
    except CollectionError as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()

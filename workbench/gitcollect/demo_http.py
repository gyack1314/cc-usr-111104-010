"""环回 HTTP 智能协议演示服务：以 CGI 方式驱动 git-http-backend。

仅绑定环回地址，仅暴露单个合成裸库目录，供收集工具演示 HTTP/HTTPS
地址解析与相对子模块地址的重新校验；不对外提供服务。
"""
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .errors import CollectionError


def serve(repo_or_root, host='127.0.0.1', port=0):
    target = Path(repo_or_root).resolve()
    if (target / 'HEAD').exists():
        project_root = target.parent
        repo_name = target.name
    elif target.is_dir():
        # 仓库根目录：暴露其下全部裸库（含嵌套子目录）
        project_root = target
        repo_name = None
        if not any(p.is_dir() and (p / 'HEAD').exists() for p in target.rglob('*.git')):
            raise CollectionError(f'目录下未发现裸仓库: {target}')
    else:
        raise CollectionError(f'不是裸仓库或仓库目录: {target}')
    if not (target / 'HEAD').exists() and not target.is_dir():
        raise CollectionError(f'不是裸仓库: {target}')
    git_http_backend = shutil.which('git-http-backend')
    if git_http_backend is None:
        # Debian 系位于 lib 目录
        candidate = '/usr/lib/git-core/git-http-backend'
        if Path(candidate).exists():
            git_http_backend = candidate
    if git_http_backend is None:
        raise CollectionError('当前 Git 安装未提供 git-http-backend，无法演示 HTTP 智能协议')

    # 服务进程的环境同样隔离全局配置与外部 helper
    base_env = {
        'PATH': os.environ.get('PATH', ''),
        'GIT_PROJECT_ROOT': str(project_root),
        'GIT_HTTP_EXPORT_ALL': '1',
        'HOME': tempfile.mkdtemp(prefix='gitcollect-http-home-'),
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_TERMINAL_PROMPT': '0',
        'REMOTE_USER': 'demo',
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _forward(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length) if length else b''
            cgi_env = dict(base_env)
            cgi_env.update({
                'REQUEST_METHOD': self.command,
                'QUERY_STRING': self.path.split('?', 1)[1] if '?' in self.path else '',
                'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                'CONTENT_LENGTH': str(length),
                'SERVER_PROTOCOL': self.request_version,
                'GATEWAY_INTERFACE': 'CGI/1.1',
                'SERVER_NAME': host,
                'SERVER_PORT': str(port),
                'PATH_INFO': self.path.split('?', 1)[0],
            })
            proc = subprocess.run([git_http_backend], input=body, capture_output=True, env=cgi_env)
            raw = proc.stdout
            header_blob, _, payload = raw.partition(b'\r\n\r\n')
            status = 200
            headers = []
            for line in header_blob.split(b'\r\n'):
                if b':' not in line:
                    continue
                key, value = line.split(b':', 1)
                key_s = key.decode().strip()
                val_s = value.decode().strip()
                if key_s.lower() == 'status':
                    status = int(val_s.split()[0])
                else:
                    headers.append((key_s, val_s))
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)

        do_GET = _forward
        do_POST = _forward

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_port = server.server_port
    return {
        'host': host, 'port': actual_port,
        'base_url': f'http://{host}:{actual_port}',
        'repo_url': f'http://{host}:{actual_port}/{repo_name}' if repo_name else None,
        'server': server,
    }

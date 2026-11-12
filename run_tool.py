#!/usr/bin/env python3
"""隔离模式入口：python3 -I run_tool.py ...，等价于 python -m workbench.cli。

-P 隔离下解释器不隐式加入脚本目录，这里显式加入本文件所在目录；
用户站点、PYTHONPATH、环境变量配置仍全部被 -I 屏蔽。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workbench.cli import main

if __name__ == '__main__':
    main(sys.argv[1:])

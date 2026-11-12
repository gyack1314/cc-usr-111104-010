# 学生源码资料的固定提交递归资料

此项目维护本地合成来源、协议资料及原始字节工具，用于复查机器人赛事与课程资料。所有名称、数据和地址都不代表真实组织或设备。

`source_lab` 提供资料服务，`sources` 保存业务样本，`workbench.raw_store` 按摘要存取原始字节。HTTP来源的每条路由可依次返回多个响应，最后一个响应会重复；服务仅绑定环回地址。详细数据格式见 [资料约定](docs/source-format.md)。

使用 Python 3.11 或以上版本，无第三方Python依赖。在项目根目录执行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q workbench source_lab tests
python -m workbench.cli inspect sources
```

需要解释器隔离（`-I`）时，根目录的 `run_tool.py` 等价于 `python -m workbench.cli`：

```sh
python -I run_tool.py demo-repos out/repos
python -I run_tool.py demo-run out/repos out/demo
python -I run_tool.py verify out/demo/pkg-before-move/package
```

## 固定提交递归收集

`workbench.gitcollect` 把学生授权的机器人项目按入口解析出的固定 commit 递归整理为可核验资料包，避免只下载默认分支 ZIP 漏掉子模块、或分支推进后拼出从未存在过的组合。

```sh
# 配置（白名单与预算）与入口（location/ref）均为 JSON
python run_tool.py collect config.json entries.json out/pkg
python run_tool.py verify out/pkg/package
```

要点：ref 开始即解析为完整40位 commit，之后 ref 移动不改变本次采集；gitlink 精确取记录的 commit 并递归嵌套子模块；同仓同 commit 多路径可复用对象获取但路径实例各自保留计数；深度、总路径数、内容字节预算可配；缺对象、缺路径声明、重复或不匹配声明整体失败；白名单比较规范化身份，相对子模块地址按 Git 语义解析后重新校验；不执行外部 remote helper、扩展协议与仓库 hook；不检出工作树、不改来源仓库；符号链接只入清单与内容文件，LFS pointer 原样保留并标明。`verify` 完全离线，重算 blob/树/commit 的 Git 对象 ID 与清单核对。设计细节见 [资料约定](docs/source-format.md)。

启动本地HTTP资料服务：

```sh
python -m source_lab.http sources/normal.json --port 8765
```

容器方式可执行 `docker build -t resource-source .`，随后 `docker run --rm resource-source python -m unittest discover -s tests -v`。服务地址只对本次运行有效，客户端应从配置取得地址。输出资料写入单独目录，原始样本保持只读。

源码来源生成器还需要 Git 2.30 或以上版本，容器已包含Git。执行 `python -m source_lab.git_sources out/repos` 在空目录生成本地裸仓库。

# 固定Git对象资料

运行 `python -m source_lab.git_sources out/repos` 会生成root.git、middle.git、leaf.git及catalog.json。root的a指向middle，b指向leaf，middle的sensor也指向leaf，形成合法共享。catalog中的initial与moved均存在于root对象库，默认main只指向initial，可用 `git -C out/repos/root.git update-ref refs/heads/main <moved>` 显式推进来源分支。生成器不包含客户端抓取或导出功能。

Git对象限定SHA-1格式，mktree与commit-tree构造对象，不执行hook。收集端需将仓库URL的规范化身份与路径实例分离，HTTP/HTTPS去默认端口及点路径，本地路径取绝对规范路径；不将本地来源目录作为输出目录。相对子模块地址以父仓库URL为目录基准按Git语义处理，例如parent=/root/root.git，../leaf.git指向/root/leaf.git。白名单比较规范化后仓库身份而非字符串前缀。gitmodules只接受path/url属性，name不参与文件落盘身份，拒绝重复path或url及非安全路径。来源地址无凭据，禁止加载全局Git配置的URL改写和外部命令。

模式100644/100755是普通blob，120000是链接目标字节，160000为gitlink，040000为子树。导出内容统一按安全内容ID命名，原路径与mode放清单；不必在本机重建软链接。对象ID为SHA1(type + 空格 + 长度 + NUL + 内容)，树采用Git原始二进制格式。有限且经过对象哈希核对的Git历史无需人为制造循环图，真实Git路径负责验证共享、精确提交、缺对象以及深度和路径预算。未承诺支持SHA256格式仓库或LFS下载。

## 收集工具（workbench.gitcollect）

`workbench.gitcollect` 在上述约束上实现固定提交递归收集，入口为仓库位置加 ref，命令行见 `python run_tool.py collect --help`，配置含 `whitelist`、`max_depth`、`max_paths`、`max_bytes`。

### 固定与解析

入口首先把 ref 解析为完整40位 commit（本地用 `rev-parse`，HTTP/HTTPS 用 `ls-remote`），本次采集随后只围绕该 commit 展开；ref 之后被推进不影响已生成的资料包。gitlink 一律取树中记录的精确 commit，绝不跟随子模块默认分支；嵌套子模块按深度递归。相对子模块地址按 Git 语义以父仓库 URL 完整路径为基准解析（每个开头 `../` 剥离一层，父仓库名本身算一层），解析结果重新规范化并再次过白名单；任何含 `::` 的 helper/扩展协议地址直接拒绝。

### 路径实例与预算

同一仓库同一 commit 出现在多个路径时，对象获取可以复用（抓取缓存与内容存储按身份去重），但每个路径实例都在清单与锁定图中独立保留，路径数与内容字节按实例计数，不存在全局 visited 误拒合法共享，也不会因对象复用漏算导出路径。递归深度、总路径数、内容字节预算均可配置，超限即整体失败。缺少对象、gitlink 缺少 `.gitmodules` 路径声明、重复 path 声明、声明路径在树中不是 gitlink、重算对象 ID 不匹配都使资料包失败。

### 隔离与只读

收集进程使用最小化环境与固定安全配置：隔离 HOME、系统与全局配置，设置 `GIT_CONFIG_NOSYSTEM`、清空 credential helper、禁用 LFS 过滤器、`core.hooksPath` 指向不存在目录，因此来源仓库自带 hook 与外部 remote helper 都不会执行；只调用 plumbing 命令读取对象，抓取进入自建裸缓存，不检出工作树，不修改来源 ref、配置或工作树。普通文件、可执行位与符号链接目标按树保留；符号链接只写入清单（`symlink_target`）和内容文件，收集端不创建链接。LFS pointer 作为普通版本文件保留并以 `lfs_pointer: true` 标明，不下载 LFS 对象。

### 资料包与离线核对

资料包写入输出目录的 `package/`：`manifest.json` 记录根 commit/根 tree、逐路径对象 ID、mode、字节数、sha256 内容键、摘要、子模块锁定图（父仓库、声明 URL、解析仓库、锁定 commit/tree、深度）以及全部接触过的 commit 原始对象；`objects/` 为 sha256 内容存储；`COMPLETED` 是最后原子写入的正式成功标记。任何中止都不会出现该标记，重开运行可复用已验证对象但不信任半成品。`python run_tool.py verify <package>` 完全离线、不调用 git、不访问来源：重算每个 blob、按仓库边界自底向上重建每棵 Git 树（树对象子树 mode 的线上格式为 `40000`）、重算 commit ID，并核对锁定图与预算计数。

### 合成演示

`python run_tool.py demo-repos <dir>` 生成本地裸库，`demo-run <repos> <out>` 依次演示分支移动、共享子模块（同一 leaf commit 五个路径实例）、单层与多层嵌套相对地址（含 `group/deep.git` 的 `../../leaf.git`）、缺失 gitlink 提交、符号链接/可执行位/LFS pointer、超预算分支、声明不匹配与重复声明、深度上限、白名单拒绝以及失败后重开复用。`http-serve <repos目录>` 经环回 HTTP 智能协议暴露同一批裸库，`demo-run --base-url` 可在 HTTP 地址上重放全部场景。所有仓库与名称均为本地合成数据。

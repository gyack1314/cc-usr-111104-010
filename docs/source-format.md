# 固定Git对象资料

运行 `python -m source_lab.git_sources out/repos` 会生成root.git、middle.git、leaf.git及catalog.json。root的a指向middle，b指向leaf，middle的sensor也指向leaf，形成合法共享。catalog中的initial与moved均存在于root对象库，默认main只指向initial，可用 `git -C out/repos/root.git update-ref refs/heads/main <moved>` 显式推进来源分支。生成器不包含客户端抓取或导出功能。

Git对象限定SHA-1格式，mktree与commit-tree构造对象，不执行hook。收集端需将仓库URL的规范化身份与路径实例分离，HTTP/HTTPS去默认端口及点路径，本地路径取绝对规范路径；不将本地来源目录作为输出目录。相对子模块地址以父仓库URL为目录基准按Git语义处理，例如parent=/root/root.git，../leaf.git指向/root/leaf.git。白名单比较规范化后仓库身份而非字符串前缀。gitmodules只接受path/url属性，name不参与文件落盘身份，拒绝重复path或url及非安全路径。来源地址无凭据，禁止加载全局Git配置的URL改写和外部命令。

模式100644/100755是普通blob，120000是链接目标字节，160000为gitlink，040000为子树。导出内容统一按安全内容ID命名，原路径与mode放清单；不必在本机重建软链接。对象ID为SHA1(type + 空格 + 长度 + NUL + 内容)，树采用Git原始二进制格式。有限且经过对象哈希核对的Git历史无需人为制造循环图，真实Git路径负责验证共享、精确提交、缺对象以及深度和路径预算。未承诺支持SHA256格式仓库或LFS下载。

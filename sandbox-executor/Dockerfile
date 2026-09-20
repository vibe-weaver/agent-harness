# 沙箱执行镜像 —— run_python 的执行载体（服务器 B 上由 executor 用 `docker run` 拉起）
#
# 设计约束（每一条都对应一个具体的坑，改之前请先读注释）：
#   1. 版本必须与 A 上生产 venv 逐字一致 → 见 requirements-sandbox.txt 顶部说明。
#      基线矩阵（后端/tests/golden_sandbox.json）是在那些版本上录的，漂移会让 golden 全部 diff。
#   2. 运行时是 `--read-only` 根文件系统。**只有 .pyc 能靠构建期烤进去受益**
#      （只被读取，不做可写性判定）；matplotlib 的字体缓存烤了也没用，它会用
#      os.access(W_OK) 判定目录可写性、只读挂载上必然 False，于是绕开缓存另起
#      临时目录并打 warning 到 stderr —— 详见下方 MPLCONFIGDIR 的注释。
#   3. UID/GID 固定 1000，必须与服务器 A 上的 blog 用户一致（阶段 3 建立）——
#      NFS 导出用的是 root_squash，A 侧工作区属主是 uid 1000，对不上就写不动。
#   4. 不装数据库驱动、Web 框架、加密/签名库（见 requirements-sandbox.txt）——
#      容器逃逸也连不上 MySQL、伪造不了 JWT。
#
# 构建（在开发机做，别在 B 的 2GB 内存里跑 pip）：
#   cd sandbox-executor
#   # 注意是 cat 后再 sha256sum：`sha256sum A B` 会输出**两行**（每个文件一行），
#   # cut 出来是两个不同的值而不是一个 tag。
#   TAG=$(cat Dockerfile requirements-sandbox.txt | sha256sum | cut -c1-12)
#   docker build --platform linux/amd64 -t blog-sandbox:$TAG .
#   docker save blog-sandbox:$TAG | gzip | ssh <B> 'gunzip | docker load'

FROM python:3.13-slim-bookworm

# slim 缺的系统库，各自解决一个具体的失败：
#   libgomp1            numpy/OpenBLAS 与 PyMuPDF 链接它，缺了 `import numpy` 直接
#                       "libgomp.so.1: cannot open shared object file"
#   tzdata              容器默认 UTC，会让 datetime.now()、PDF 页脚、生成的文件名全变
#                       （运行时还会传 -e TZ=Asia/Shanghai，但没有 tzdata 那个变量无效）
#   fontconfig          **对 matplotlib 并非必需**（查过源码）：font_manager.py:148-158
#                       的 X11FontDirectories 已硬编码 "/usr/share/fonts/"，靠目录遍历
#                       就能发现 wqy；而 _get_fontconfig_fonts()（:254-265）在 fc-list
#                       缺失时 except OSError → return []，**静默**，不打 warning。
#                       显式装它的真实理由有两条：fonts-wqy-microhei 的 Depends 本来
#                       就会把它带进来（--no-install-recommends 不裁 Depends），以及
#                       fc-list 是线上排查"为什么中文没渲染"的唯一顺手工具。
#   fonts-wqy-microhei  提供 /usr/share/fonts/truetype/wqy/wqy-microhei.ttc 与
#                       family "WenQuanYi Micro Hei" —— 与 tool_registry.py:1243 的
#                       PDF 候选路径和 :908 的 matplotlib 字体列表逐字对齐。
#                       缺了中文 PDF 出豆腐块、图表中文变方框。
#   fonts-dejavu-core   提供 /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf ——
#                       tool_registry.py:1253 的等宽候选。matplotlib 自带 DejaVu，
#                       但 reportlab 是按**绝对路径**读的，必须系统里有。
# 刻意不装 fonts-noto-cjk：wqy 已覆盖 CJK 且体积小得多（Noto ~60MB）。
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgomp1 \
      tzdata \
      fontconfig \
      fonts-wqy-microhei \
      fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# UID/GID 必须与服务器 A 上的 blog 用户一致（阶段 3.1 用 `useradd -r -u 1000 blog` 建立）
RUN groupadd -g 1000 sandbox && useradd -u 1000 -g 1000 -m -d /home/sandbox sandbox

COPY requirements-sandbox.txt /tmp/requirements-sandbox.txt
RUN pip install --no-cache-dir -r /tmp/requirements-sandbox.txt \
    && rm /tmp/requirements-sandbox.txt

# MPLBACKEND 必须在 import pyplot 之前就是 Agg，否则构建期会去探测 GUI 后端并告警。
#
# MPLCONFIGDIR **不能**指向镜像里的目录（原计划是 /opt/mplcache + 构建期烤缓存），
# 因为 matplotlib/__init__.py:567-599 的 _get_config_or_cache_dir() 是这么判的：
#     configdir.mkdir(parents=True, exist_ok=True)   # 目录已存在 → EROFS 被 exist_ok 吞掉
#     if os.access(str(configdir), os.W_OK) and configdir.is_dir(): return ...
#     _log.warning("%s is not a writable directory", configdir)
#     tmpdir = tempfile.mkdtemp(prefix="matplotlib-"); _log.warning("Matplotlib created ...")
# os.access(W_OK) 在只读挂载上对**任何**属主都是 False，所以烤进去的缓存根本不会被读，
# 还会白打两条 WARNING 到 stderr。这两条警告在"用户代码没有 stdout"时会按生产
# tool_registry.py:1770 的 `if not output and err_lines` 顶替成工具结果喂给 LLM，
# 里面还带着 /opt/mplcache 这种宿主路径。
#
# 指向 /tmp 就完全不触发：executor 与测试矩阵都用 --tmpfs /tmp:size=32m 挂载，
# 运行时可写。至于"每次重扫字体"的代价——实测 matplotlib 3.11.1 在本机 646 个字体
# 上重建 fontlist 全程 0.54s（含 import），本镜像只有 ~62 个字体文件
# （matplotlib 自带 mpl-data/fonts/ttf 40 + dejavu-core ~21 + wqy-microhei 1），
# 扫描部分是 0.04s 量级。计划里"退回临时目录会 +1~3s"的说法来自桌面机几百个字体的
# 场景，在 slim 镜像上不成立。XDG_CACHE_HOME 同理：fontconfig 的用户缓存默认落
# $HOME/.cache/fontconfig，那在只读根上也是写不动的。
ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mplcache \
    XDG_CACHE_HOME=/tmp/.cache

# 全量 .pyc —— 这个在 --read-only 下是**真的**省：.pyc 只被读取、不做可写性判定，
# 缺了每次都要现场重编译 numpy/pandas。必须在 PYTHONDONTWRITEBYTECODE 之前跑。
#
# 第二条是构建期冒烟测试，覆盖 _ALLOWED_MODULES 里全部 13 个第三方库：缺 libgomp1
# 会让 `import numpy` 报 "libgomp.so.1: cannot open shared object file"，缺字体包
# 会让 reportlab 找不到中文/等宽字体文件。这类问题应该在 build 阶段就炸掉，
# 而不是等到线上第一次 run_python 才炸。
#
# 字体断言刻意校验**绝对路径**而不是 family 名：tool_registry.py:1243/:1253 的
# reportlab 是按绝对路径 open 字体文件的，路径不在就是中文 PDF 直接崩；而 rcParams
# 用的是 family 名（:908），那条链末尾还有 DejaVu Sans 兜底，对不上只影响图表里的
# 中文（golden 矩阵的 matplotlib 用例会抓到），不值得让 build 失败。
# CJK family 名照打出来，构建日志里可以直接核对是不是 WenQuanYi Micro Hei。
#
# 依赖 import 列表必须覆盖 requirements-sandbox.txt 里的**每一个**包：
# 只抽查几个核心库会漏掉"装了但 import 不到"的包（charset_normalizer/pytz/packaging/lxml
# 就曾不在这个列表里）。tests/test_sandbox_executor.py 有测试比对这张列表与
# requirements 是否一一对应，加包忘了改这里会红。
# 不 import fitz：它是 PyMuPDF 的弃用 shim，import 时会往 stdout 打一行告警刷构建日志；
# 沙箱内 `import fitz` 已被 _safe_import 直接别名到 pymupdf，所以只需验 pymupdf。
RUN python -m compileall -q /usr/local/lib/python3.13/site-packages && python -c "import matplotlib.pyplot, matplotlib.font_manager as fm, numpy, pandas, PIL, openpyxl, docx, reportlab, pptx, tabulate, chinese_calendar, xlsxwriter, pymupdf, yaml, markdown, jinja2, dateutil, charset_normalizer, pytz, packaging, lxml, fontTools, markupsafe, pyparsing, cycler, contourpy, kiwisolver, et_xmlfile, six, tzdata, typing_extensions; seen={f.fname for f in fm.fontManager.ttflist}; need=['/usr/share/fonts/truetype/wqy/wqy-microhei.ttc','/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf']; missing=[p for p in need if p not in seen]; assert not missing, ('系统字体没被发现，中文 PDF 会崩: '+str(missing)); print('SMOKE_OK fonts=', len(seen), 'cjk=', sorted({f.name for f in fm.fontManager.ttflist if 'Hei' in f.name or 'CJK' in f.name or 'Song' in f.name}))" && rm -rf /tmp/mplcache /tmp/.cache

# 运行时不写 .pyc（根文件系统只读，写了也是失败 + 告警噪声）
ENV PYTHONDONTWRITEBYTECODE=1

USER 1000:1000
WORKDIR /workspace

# 脚本走 **stdin**，不落盘、不 bind mount：
#   - B 的磁盘上永远不出现用户代码
#   - 绕开 SELinux 对本地文件的 label 要求（NFS 挂载用 context= 另说）
# -I 与生产 tool_registry.py:1722 逐字一致（隔离模式：忽略 PYTHON* 环境变量、
#    不加脚本目录到 sys.path、不加载 user site）。
# 模板不用 __file__ / sys.argv（沙箱 globals 里 __file__ 被显式设为 None，
# 见 tool_registry.py:1509），所以 stdin 与临时文件两种载体行为等价。
ENTRYPOINT ["python", "-I", "-X", "utf8", "-"]

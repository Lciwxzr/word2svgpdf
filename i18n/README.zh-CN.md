# word2svgpdf

**把 Word 导出 PDF 时被栅格化的图，换回矢量版本。**

> **版本说明。** 本文件是原始版本。仓库根目录的英文 README 由
> **DeepSeek-V4.1-Flash** 翻译，若两者有出入，以本文件为准。
> → [English (translated)](../README.md)

把 Word 导出 PDF 时被栅格化的图，换回插入 word 时候的矢量版本，并逐页校验其余内容没有改动。

## 原理

**Word 中插入的 SVG 矢量图，其原始 SVG 文件可以通过解压 docx 获得。**
采用 Inkscape 把该 SVG 导出为 PDF 后，即可匹配 PDF 中对应的位图进行替换。

具体来说，`.docx` 本质上是一个 zip 包。Word 插入 SVG 时，会把两个文件同时存进去：

```
word/media/image1.png      ← Word 自己渲染的位图（备用回退图）
word/media/image2.svg      ← 你插入的原始 SVG 矢量文件
```

两者在 `document.xml` 里一一配对：`<a:blip>` 指向 PNG，`<asvg:svgBlip>` 指向 SVG。
所以**原始矢量数据从未丢失**，只是 Word 导出 PDF 时没用它。

导出 PDF 时，如果 svg 中有位图元素，那么这张图就会变成一张固定分辨率的位图。本工具做的是：

1. 解压 docx，取出每张图对应的原始 SVG
2. 用 Inkscape 把该 SVG 转成矢量 PDF
3. 在导出的 PDF 里找到被栅格化的那张位图（按文档顺序 + 尺寸 + 图像内容匹配）
4. 把矢量 PDF 叠加到同一位置，并把旧位图设为完全透明

这样得到的 PDF，图恢复成真矢量（文字可选中、放大不模糊），而图区之外的正文、图注、页码全部保持原样。

## 为什么需要它

Word 对 SVG 的支持一直是"打补丁式"的：**只要 SVG 内部嵌有 `<image>` 位图元素，Word 就会把整张图降级成一张位图**。判断依据很明确，本仓库实测 14 张图全部符合：

| SVG 内嵌位图数 | Word 导出结果 |
|---|---|
| 0 个 | 保持矢量 |
| ≥1 个 | 整张栅格化（通常压到 266 dpi） |

所以真正需要替换的往往只有少数几张带有系统图/示意图的图，而不是全部。

## 怎么用

```
setup.bat     :: 第一次运行，建环境（约 2 分钟，需联网）
start.bat     :: 之后每次运行，启动工具
```

双击 `start.bat`，窗口里会打印服务地址，并自动把地址复制到剪贴板——切到浏览器按 Ctrl+V 即可。

端口是自动选的：从 8770 起依次尝试，跳过被占用或被系统保留的端口（Windows 会为 Hyper-V/WSL/Docker 保留整段端口）。

**默认不会自动打开浏览器。** Windows 上如果浏览器已经以管理员权限运行，普通进程无法把 URL 交给它（会弹出 "Edge isn't responding becausean existing instance is running with elevated privileges"）。想要自动打开，设置环境变量 `VECTORFIG_OPEN_BROWSER=1`。

如果环境还没建好，`start.bat` 会自动调用 `setup.bat`。

离线机器：在有网络的机器上先跑一次 `make_wheels.bat`，把整个文件夹（含 `wheels\`）拷过去，之后 `setup.bat` 全程不需要网络。

### 两段式流程

界面刻意分成两步，让你在付出 Inkscape 转换的时间之前先检查匹配是否正确：

```
上栏 · Word 文档  ┐
                  ├─→  ① 匹配图  →  检查/修改  →  ② 开始转换  →  *_vec.pdf
下栏 · 导出的 PDF ┘
```

1. **① 匹配图**——上传 `.docx` 和它导出的 `.pdf`。匹配只要几毫秒到几秒，
   然后出一张表，每张需要处理的图占一行。
2. **检查并修改**——每行展开后是左右两栏：
   - **左侧是参考图**：Word 塞进 PDF 的那张位图，用它判断该放哪张图
   - **右侧是候选**：PDF 里每个可能的位图位置，带缩略图、页码、相似度
   - 点任意候选即可把图指定到那里
   - 点「跳过」不替换这张
   - 拖一个 `.svg` 到候选列表末尾的虚线框，用外部矢量图替换这张的源图
   - 改错了点「撤销」回到自动匹配的结果
3. **② 开始转换**——只有点了这一步才跑 Inkscape导出PDF（每张约 13 秒），
   完成后出 PDF 和校验报告。
4. **改完重转**——转换后发现映射错了，改完直接再点「② 开始转换」。
   已转换的矢量 PDF 会被复用，只重新叠加，几秒完成。

### 转换完成后可以

- **另存为**：点击按钮会弹出系统「另存为」对话框（Chrome/Edge 支持；不支持的浏览器自动回退为普通下载）
- **逐页对比**：整页左右对比，可用扫描线 / 半透明叠层 / 左右并排三种模式
- **图元素对比**：只看被替换的图区，放大校对
- **校验报告**：程序自动逐页比对，明确告诉你图区之外有没有变化

### 内嵌照片分辨率

输出区有一个下拉框，**默认「保持原样（不重压缩）」**：

| 选项 | 说明 |
|---|---|
| 保持原样（默认） | 位图保持原始分辨率，只换图不改画质 |
| 600 dpi | 体积与清晰度均衡 |
| 300 dpi | 最小体积，仍满足多数期刊 |
| 900 / 1200 dpi | 更高清晰度 |

## 图区匹配依据

匹配主要为三路信号独立打分，用匈牙利算法做**全局一对一分配**：

| 信号 | 强度 | 说明 |
|---|---|---|
| **图像内容** | 最强 | docx 里每张图旁都存有 Word 自己渲染的 PNG；把它与 PDF 里的位图都合成到白底后做相关性比对。实测正确配对 **+1.000**，错误配对 **-0.06** |
| 显示尺寸 | 中 | docx 的 EMU 尺寸 vs PDF 的放置矩形，容差 4 pt |
| 文档顺序 | 弱（仅决胜） | 所有图都是 inline，Word 按文档顺序输出，PDF 也按阅读顺序排列 |

匹配依据会写进校验报告的「说明」列，例如
`第 10 页（图像相似度 1.00，尺寸吻合）`。

### 手动干预

自动匹配不是最终决定。在「① 匹配图」之后、点「② 开始转换」之前，
每张图都可以手动修改：

| 操作 | 效果 |
|---|---|
| 点候选缩略图 | 把这张图指定到那个位图位置 |
| 跳过 | 这张图不替换，保留 Word 导出的位图 |
| 撤销 | 回到自动匹配的结果 |
| 拖入 SVG | 用外部 `.svg` 替换这张的源图（当 docx 里没有可用 SVG，或想换一张图时用） |
| 恢复 docx 源图 | 撤销自定义 SVG |

手动指定的行会高亮，说明列写「用户手动指定」。改完点「② 开始转换」生效。

### 缩放规则

替换进去的矢量图会**等比缩放到原图矩形内**，不会越界压到正文、图注或相邻的图：

```
缩放比例 = min(原图宽 / 矢量宽, 原图高 / 矢量高)
```

结果居中放置，且至少有一个方向恰好贴合原图边界。

例：原图 700×900，矢量图 800×1200 → 缩放 0.75 → 放置 600×900（高度恰好贴合，宽度 600 小于 700，完全在框内）。

这样处理是因为：非等比缩放会让图变形；而任何大于框的缩放都会盖住周围内容。

## 环境要求

- Python 3.10+
- [Inkscape](https://inkscape.org/)（**必需**，用于 SVG → 矢量 PDF 转换）

`start.bat` 会自动定位 Python 和 Inkscape、检查并按需安装 `requirements.txt` 里的依赖。

Inkscape 不在默认路径时，设置环境变量 `INKSCAPE` 指向 `inkscape.exe`。

### 依赖与许可证

| 包 | 许可证 | 用途 |
|---|---|---|
| pypdfium2 | Apache-2.0 / BSD-3-Clause | 渲染、文字提取、图像定位 |
| pikepdf | MPL-2.0 | PDF 对象级编辑，保留对象编号 |
| starlette | MIT | HTTP 框架 |
| uvicorn | BSD-3-Clause | ASGI 服务器 |
| python-multipart | Apache-2.0 | 表单上传 |
| Pillow | HPND | 位图处理、图像签名 |
| lxml | BSD-3-Clause | docx 和 SVG 解析 |
| scipy | BSD-3-Clause | 匈牙利算法（可选，缺失时退化为贪心） |

外部程序：

| 程序 | 许可证 | 用途 |
|---|---|---|
| Inkscape 1.x | GPL-2.0-or-later | SVG → 矢量 PDF |

Inkscape 通过命令行作为独立程序调用，不链接其代码，不构成衍生作品。
如果把 Inkscape 一起分发，GPL-2.0 要求同时提供其源码。

### `setup.bat` 到底做了什么

这是一个会在你机器上自动配置环境的脚本，所以把它的全部行为列清楚。**它不需要管理员权限，也不修改系统。**

**只读取这些位置：**

| 位置 | 用途 |
|---|---|
| `%VECTORFIG_PYTHON%`，其次 `PATH` 上的 `python.exe`，再其次 `%USERPROFILE%\miniconda3`、`%USERPROFILE%\anaconda3`、`%LOCALAPPDATA%\Programs\Python\Python31{0,1,2,3}`、`C:\ProgramData\{mini,}conda3` | 找一个 3.10+ 的 Python 解释器 |
| `C:\Program Files\Inkscape`、`C:\Program Files (x86)\Inkscape`、`%LOCALAPPDATA%\Programs\Inkscape`、`env\inkscape`、`%INKSCAPE%` | 找 `inkscape.exe` |
| `requirements.txt`、`wheels\` | 依赖清单和可选的离线缓存 |

它只通过找到的那个解释器执行下列命令：

| 命令 | 用途 |
|---|---|
| `python -c "import sys; sys.exit(...)"` | 检查版本是否 ≥ 3.10 |
| `python --version`、`inkscape.exe --version` | 报告找到的版本 |
| `python -m venv env\venv` | 创建隔离环境 |
| `env\venv\Scripts\python.exe -m pip install -r requirements.txt` | 安装固定版本的包 |

脚本里没有 `powershell`、`curl`、`reg`、`setx`、`schtasks`
或任何其他修改系统的命令。

**只写入这些位置：**

| 位置 | 内容 |
|---|---|
| `env\venv\`（项目目录内） | Python 虚拟环境 |
| `%LOCALAPPDATA%\vectorfig\venv\` | 同上，**仅当**第一个位置失败时（例如 OneDrive 锁住目录） |
| 该 venv 的 `Lib\site-packages\` | `requirements.txt` 里列出的包 |

**它绝不会**：改注册表、改 `PATH` 或任何环境变量、装服务或计划任务、
请求提权、写到上述两个位置之外、或下载 `requirements.txt` 之外的任何东西
（且只从 PyPI 或本地的 `wheels\` 目录获取）。

要完全撤销，删掉 `env\` 即可（如果用了回退位置，再删 `%LOCALAPPDATA%\vectorfig\`）。
机器上不会留下其他东西。

如果你不想运行批处理文件，等价的手动操作是：

```bat
python -m venv env\venv
env\venv\Scripts\python.exe -m pip install -r requirements.txt
start.bat
```

`start.bat` 本身只做三件事：启动本地服务、打印地址、把地址复制到剪贴板；
只有设置了 `VECTORFIG_OPEN_BROWSER=1` 才会打开浏览器。

## 开源与许可

本项目采用 **AGPL-3.0**。

需要说明的是：PDF 层用的 pypdfium2（Apache-2.0/BSD）和 pikepdf（MPL-2.0）都是宽松许可，本身不要求 AGPL。选择 AGPL-3.0 是为了保证这个工具及其衍生版本始终对使用者开放——它处理的是科研稿件，作者有权知道它做了什么。

如果你需要在闭源项目里使用，可以改用 MIT 许可（本项目代码本身允许，只需确认没有引入 GPL 组件；Inkscape 通过命令行调用，不构成衍生作品）。

Inkscape 通过命令行调用，不链接其代码，不构成衍生作品。

## 校验逻辑

对每一页：

1. 把被替换的图区矩形遮白
2. 比对剩余区域的**文字**（含坐标）
3. 比对剩余区域的**渲染像素**

判定：图区之外 0 差异 = 内容未变。任何真实差异都会在报告里标红并指出具体缺了哪个词。

## 文件结构

```
app.py             HTTP 接口（两段式流程、手动指定、自定义 SVG）
core.py            流水线：analyse / convert / compose + 校验
smart_match.py     图区匹配（顺序 → 尺寸 → 内容，匈牙利算法）
pdfio.py           PDF 读取 —— pypdfium2（渲染、文字、图像定位）
pdfedit.py         PDF 写入 —— pikepdf（叠加、透明化旧图、保留对象编号）
downsample.py      可选的位图降采样，保留透明度
run.py             启动器（自动选端口、复制地址到剪贴板）
static/index.html  单页界面前端
setup.bat          建环境
start.bat          启动
make_wheels.bat    缓存依赖，供离线使用
collect_wheels.py  从已安装的包重建 wheel，不需要网络
```

实现细节和设计取舍记录在作者本地的 `DEVELOPMENT.md` 中，不随仓库发布。

命令行用法（不开界面）：

```python
from core import Progress, run_pipeline
rep = run_pipeline("in.docx", "in.pdf", "out.pdf", Progress(print))
print(rep["ok"], rep["size_after"])
```

需要分阶段控制时用 `Session`：

```python
from core import Progress, Session

sess = Session("in.docx", "in.pdf", "workdir")
sess.analyse(Progress(print))     # 只匹配，几毫秒
# 此时可以改 sess.figures[i].override 做手动指定
sess.convert(Progress(print))     # 只转换，约 13 秒/张
rep = sess.compose("out.pdf", Progress(print))
```

## 已知限制

- 图区匹配优先用「文档顺序 + 尺寸」。只有当图是浮动定位、或数量对不上时才走内容比对；仍无法唯一确定时标记为「待确认」，此时可以在界面上手动指定，不会被静默跳过。
- 替换后文件体积会增大（照片从 Word 压缩的 266 dpi 恢复到原始分辨率）。默认选择「保持原样」不做任何重压缩；需要小体积时在下拉框里选 300/600 dpi。
- 「另存为」对话框需要 Chrome/Edge 等支持 File System Access API 的浏览器，且必须通过 `http://127.0.0.1` 访问（localhost 属于安全上下文）。其他情况自动回退为普通下载。
- 默认不自动打开浏览器（见上文说明），地址会复制到剪贴板。
- 写入用 pikepdf 的对象级编辑，原有对象编号全部保留，但被替换页的旧位图仍以透明形式留在文件里（不渲染、占少量体积）。这是保留对象编号的代价。
- Inkscape 无法自动安装：官网对自动化请求返回 HTTP 错误，且下载页是 JS 渲染的。需要用户手动装一次（约 5 分钟）。

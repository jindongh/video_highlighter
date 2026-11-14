# 🏀 Rainier 篮球视频集锦自动生成系统 (Basketball Video Highlighter)

专为 **Rainier（39号球衣）** 篮球比赛打造的端到端智能集锦制作系统。支持从多机位（Google Pixel、DJI Action 5 Pro、iPhone）通过 Syncthing 同步的原始视频中，借助 **Google Gemini 3.8 Flash** 多模态 AI 自动检测 39 号 Rainier 的进球、快攻、抢断、盖帽与精彩攻防瞬间，并将分析结果持久化存入 **SQLite3** 数据库，支持在几秒内生成**单场、季度、年度高光集锦**。

---

## 💡 为什么需要这个系统？

- **多机位自动汇总**：DJI Action 5 固定机位全场录制、Pixel/iPhone 手持特写同步汇总。
- **免除慢速 CPU 转码**：Action 5 的 2.7K/4K 60fps 高清原片若用 CPU 软转码极其缓慢（常耗费 20+ 分钟）。本系统**彻底废弃本地 CPU 压缩**，视频直传 Google Gemini Files API（0% CPU 占用），剪切则采用**无损流复制（0.4 秒切片）**或**免转码 HTML5 播放器**。
- **分析一次，永久复用**：比赛视频经 AI 识别后，所有高光事件、时间戳、球员标注均自动存入 SQLite 数据库。未来生成季度、年度集锦时，**0 秒分析、0 API 调用**。
- **严格时序对齐**：通过 `ffprobe` 提取 EXIF / QuickTime 真实拍摄时间（及 DJI/Pixel 文件名时间回退），确保跨机位剪辑严格还原比赛时间线。

---

## 🏗️ 整体工作流架构

```
[DJI Action 5] ──┐
[Google Pixel] ──┼─> Syncthing ─> 树莓派 / 本地储存 (~/syncthing/...)
[Apple iPhone] ──┘                      │
                                        ▼
                           highlighter.py 扫描并解析 EXIF
                                        │
                         ┌──────────────┴──────────────┐
                         ▼                             ▼
                 新视频（未分析）              已存在于 SQLite
                         │                             │
                         ▼                             │
            直传 Gemini 3.8 Flash API                  │
           （多模态识别 #39 Rainier 高光）              │
                         │                             │
                         ▼                             │
                 写入 SQLite3 数据库 (highlighter.db)   │
                         │                             │
                         └──────────────┬──────────────┘
                                        │
                                        ▼
                           选择集锦输出形式：
                 ┌──────────────────────┴──────────────────────┐
                 ▼                                             ▼
       【HTML5 交互式播放器】                         【ffmpeg 无损流合并】
          (python3 ... --html)                        (python3 ... -o reel.mp4)
         • 0% CPU，无需视频剪辑                         • 使用 -c copy 流复制
         • 浏览器自动跳过垃圾时间                       • 0.4 秒/片段，极速完成
         • 实时筛选 #39 Rainier 高光                    • 可混音背景音乐 (BGM)
```

---

## ✨ 核心特性

1. **专注 39 号球员 Rainier**：
   - 提示词与结构化 Schema 专为 39 号定制，精准捕捉得分（中投、突破、三分）、助攻、抢断、大帽及庆祝镜头。
2. **0% CPU 直传 Gemini**：
   - 免费版 Gemini Files API 支持单文件最大 2.0GB。Action 5 五分钟 1.7GB 视频直接无损上传分析，避免本地 CPU 软压耗时。
3. **SQLite3 多周期持久化**：
   - 支持 `--from-db`，轻松跨越时间维度提取：
     - **单场比赛集锦**：赛后即刻生成。
     - **季度赛季集锦**：筛选近 3 个月得分 $\ge 6$ 分的关键球。
     - **年度巅峰集锦**：筛选全年得分 $\ge 8$ 分的最具统治力镜头。
4. **零等待 HTML5 Web 播放器 (`--html`)**：
   - 一键生成独立播放网页 `highlights.html`，直接播放原片并自动在时间戳之间跳转，跳过所有死球/停滞时间，支持卡片点击定位与球员筛选。
5. **极速无损剪辑 (`-c copy`)**：
   - 如果需要导出独立 MP4 视频，采用无损数据流复制，剪切一段只需 **0.4 秒**。

---

## 📦 安装与配置

### 1. 系统要求
- Python 3.10+
- 系统已安装 `ffmpeg` 和 `ffprobe`（Ubuntu: `sudo apt install -y ffmpeg`）

### 2. 安装 Python 依赖
```bash
pip install -r requirements.txt
```

### 3. 配置 Gemini API Key
前往 [Google AI Studio](https://aistudio.google.com/) 获取免费的 API Key，然后设置环境变量：
```bash
export GEMINI_API_KEY="你的_GEMINI_API_KEY"

# 或写入 ~/.bashrc 永久生效
echo 'export GEMINI_API_KEY="你的_GEMINI_API_KEY"' >> ~/.bashrc
```

---

## 🚀 常用操作指南

### 1. 扫描新视频并仅做 AI 分析（入库存储）
如果平时同步了比赛视频，只希望先让 AI 分析并存进数据库，不消耗时间剪辑：
```bash
python3 highlighter.py \
  -i ~/syncthing/jindongh/DJI \
  -i ~/syncthing/jindongh/Pixel \
  --analyze-only
```

### 2. 瞬间生成 HTML5 交互式集锦播放器（0 秒等待，推荐）
直接读取数据库，生成网页播放器，在浏览器中观看 Rainier 的所有高光时刻，自动跳过垃圾时间：
```bash
python3 highlighter.py --from-db --rainier-only --html
```

### 3. 生成单场比赛 MP4 视频集锦
扫描目录，自动将新视频入库，并利用无损流复制合并为高光视频：
```bash
python3 highlighter.py \
  -i ~/syncthing/jindongh/DJI \
  --rainier-only \
  -o ~/highlights/game_20261107.mp4
```

### 4. 从数据库提取“季度集锦”（无需重新分析）
指定日期范围与最低精彩度（如 6 分以上），提取一整个季度的集锦：
```bash
python3 highlighter.py \
  --from-db \
  --after 2026-09-01 \
  --before 2026-11-30 \
  --min-excitement 6 \
  --rainier-only \
  -m ~/music/hype_beat.mp3 \
  -o ~/highlights/season_q3_rainier.mp4
```

### 5. 从数据库提取“年度最佳镜头”
年底制作年度回顾，筛选评分 $\ge 8$ 分的顶级高光：
```bash
python3 highlighter.py \
  --from-db \
  --after 2026-01-01 \
  --before 2026-12-31 \
  --min-excitement 8 \
  --rainier-only \
  -o ~/highlights/rainier_2026_annual_best.mp4
```

### 6. 查看精彩库统计数据
随时查看数据库中累积了多少视频、多少场球赛、多少个 Rainier 39号的精彩镜头：
```bash
python3 highlighter.py --db-stats
```
输出样例：
```text
📦 SQLite Database Statistics (highlighter.db):
  • Total videos stored     : 12
  • Basketball game videos  : 11
  • Total highlight moments : 48
  • Rainier (#39) highlights: 35
  • Date range covered      : 2026-09-15 ~ 2026-11-07
```

---

## 🛠️ CLI 完整参数清单

| 参数 | 缩写 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `--input` | `-i` | (可选) | 输入视频目录或文件路径，支持多次指定多个目录 |
| `--output` | `-o` | `highlights.mp4` | 最终生成的视频文件路径 |
| `--from-db` | | 关 | **从 SQLite 数据库直接提取**，跳过目录扫描与 AI 分析 |
| `--html` | | 关 | **导出交互式 HTML5 播放器** (`highlights.html`)，0 CPU 无需剪视频 |
| `--db-stats` | | 关 | 打印 SQLite 库内视频数、高光片段及时间跨度统计 |
| `--db` | | `highlighter.db` | SQLite 数据库文件路径 |
| `--rainier-only` / `--all-players` | | `--all-players` | 是否专挑 39 号 Rainier 的精彩镜头 |
| `--after` | | 无 | 过滤拍摄起始日期 (`YYYY-MM-DD`) |
| `--before` | | 无 | 过滤拍摄截止日期 (`YYYY-MM-DD`) |
| `--min-excitement` | | `5` | 最低精彩度阈值 (1-10分) |
| `--sort` | | `chronological` | 剪辑顺序：`chronological` (按比赛时间) 或 `excitement` (按高分倒序) |
| `--music` | `-m` | 无 | 可选背景音乐文件 (如 `.mp3`)，自动循环并淡出 |
| `--buffer` | | `2.0` | 精彩镜头前后保留的缓冲秒数 |
| `--max-clip` | | `15.0` | 单个镜头的最大时长上限（秒） |
| `--analyze-only` | | 关 | 仅执行 AI 分析并写入 SQLite，不执行剪辑 |
| `--api-key` | | 读取环境变量 | Google Gemini API Key |

---

## 📂 项目结构

```
video_highlighter/
├── highlighter.py      # 主程序 CLI 入口（支持扫描、查询、剪切、HTML导出）
├── analyzer.py         # Google Gemini 3.8 Flash Interactions API 多模态分析器
├── db.py               # SQLite3 数据库管理模块（持久化存储与多维度查询）
├── player.py           # 交互式 HTML5 零剪切高光网页播放器生成器
├── editor.py           # ffmpeg 极速无损流复制 (-c copy) 剪辑与混音模块
├── scanner.py          # 视频扫描器（ffprobe 解析 EXIF 时序、格式及属性）
├── models.py           # Pydantic 核心数据模型 (VideoInfo, Highlight 等)
├── requirements.txt    # Python 依赖包清单
└── README.md           # 项目使用与配置说明文档
```

---

## 📄 License
MIT License

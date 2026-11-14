"""Interactive HTML5 Highlight Player generator.

Creates a zero-processing interactive web player for basketball highlights.
Allows instant viewing of all highlights with auto-skip of downtime,
requiring NO video re-encoding or ffmpeg slicing.
"""

from __future__ import annotations

import json
from pathlib import Path
from models import AnalysisResult


def generate_html_player(analysis: AnalysisResult, output_html: str = "highlights.html", title: str = "Rainier Basketball Highlights") -> str:
    """Generate a modern, responsive HTML5 basketball highlight player."""
    out_path = Path(output_html).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Flatten highlights with video info
    playlist = []
    for video in analysis.videos:
        for h in video.highlights:
            playlist.append({
                "video_path": video.video_path,
                "video_name": Path(video.video_path).name,
                "start": max(h.start_time - 1.5, 0.0),
                "end": h.end_time + 1.5,
                "type": h.highlight_type if isinstance(h.highlight_type, str) else h.highlight_type.value,
                "description": h.description,
                "score": h.excitement_score,
                "rainier": getattr(h, "involves_rainier", False),
            })

    # Sort: Rainier first, then score descending, or chronological
    playlist.sort(key=lambda x: (1 if x["rainier"] else 0, x["score"]), reverse=True)
    playlist_json = json.dumps(playlist, ensure_ascii=False, indent=2)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card: #1e293b;
      --accent: #f59e0b;
      --rainier: #3b82f6;
      --text: #f8fafc;
      --text-muted: #94a3b8;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }}
    header {{
      padding: 12px 24px;
      background: #020617;
      border-bottom: 1px solid #334155;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    header h1 {{ font-size: 1.25rem; font-weight: 700; color: var(--accent); }}
    .badge {{
      background: #1e293b;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 0.85rem;
      border: 1px solid #475569;
    }}
    .main-container {{
      display: flex;
      flex: 1;
      overflow: hidden;
    }}
    .player-section {{
      flex: 3;
      display: flex;
      flex-direction: column;
      background: #000;
      justify-content: center;
      position: relative;
    }}
    video {{
      width: 100%;
      height: 100%;
      max-height: calc(100vh - 120px);
      object-fit: contain;
    }}
    .sidebar {{
      flex: 2;
      background: var(--card);
      border-left: 1px solid #334155;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    .sidebar-header {{
      padding: 14px 18px;
      border-bottom: 1px solid #334155;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .playlist {{
      overflow-y: auto;
      flex: 1;
      padding: 8px;
    }}
    .clip-card {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 8px;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .clip-card:hover {{
      border-color: var(--accent);
      transform: translateY(-1px);
    }}
    .clip-card.active {{
      border-color: var(--accent);
      background: #1e1b4b;
      box-shadow: 0 0 12px rgba(245, 158, 11, 0.25);
    }}
    .clip-card.is-rainier {{
      border-left: 4px solid var(--rainier);
    }}
    .clip-header {{
      display: flex;
      justify-content: space-between;
      margin-bottom: 6px;
      font-size: 0.85rem;
    }}
    .tag-rainier {{
      background: var(--rainier);
      color: #fff;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 600;
    }}
    .tag-type {{
      background: #334155;
      color: #e2e8f0;
      padding: 2px 6px;
      border-radius: 4px;
      text-transform: uppercase;
      font-size: 0.75rem;
    }}
    .clip-desc {{
      font-size: 0.9rem;
      line-height: 1.4;
      color: #cbd5e1;
    }}
    .controls {{
      padding: 12px 18px;
      border-top: 1px solid #334155;
      display: flex;
      gap: 10px;
    }}
    .btn {{
      background: var(--accent);
      color: #000;
      font-weight: 600;
      border: none;
      border-radius: 6px;
      padding: 8px 16px;
      cursor: pointer;
      font-size: 0.9rem;
      transition: opacity 0.2s;
    }}
    .btn:hover {{ opacity: 0.9; }}
    .btn-secondary {{
      background: #334155;
      color: #fff;
    }}
  </style>
</head>
<body>
  <header>
    <h1>🏀 Rainier Basketball Highlight Hub</h1>
    <span class="badge" id="clipCount">共 {len(playlist)} 个精彩瞬间</span>
  </header>

  <div class="main-container">
    <div class="player-section">
      <video id="mainVideo" controls autoplay playsinline></video>
    </div>

    <div class="sidebar">
      <div class="sidebar-header">
        <h3>精彩镜头列表</h3>
        <label style="font-size:0.85rem; cursor:pointer;">
          <input type="checkbox" id="autoNext" checked> 连播跳过垃圾时间
        </label>
      </div>

      <div class="playlist" id="playlistContainer"></div>

      <div class="controls">
        <button class="btn" id="playAllBtn">▶ 播放全场集锦</button>
      </div>
    </div>
  </div>

  <script>
    const clips = {playlist_json};
    const video = document.getElementById('mainVideo');
    const container = document.getElementById('playlistContainer');
    const autoNextCheckbox = document.getElementById('autoNext');
    let currentIndex = 0;

    function renderPlaylist() {{
      container.innerHTML = '';
      clips.forEach((clip, idx) => {{
        const card = document.createElement('div');
        card.className = 'clip-card' + (clip.rainier ? ' is-rainier' : '') + (idx === currentIndex ? ' active' : '');
        card.onclick = () => playClip(idx);

        const minutes = Math.floor(clip.start / 60);
        const seconds = Math.floor(clip.start % 60).toString().padStart(2, '0');

        card.innerHTML = `
          <div class="clip-header">
            <div>
              ${{clip.rainier ? '<span class="tag-rainier">#39 RAINIER</span> ' : ''}}
              <span class="tag-type">${{clip.type}}</span>
            </div>
            <span style="color:var(--accent);">★ ${{clip.score}}/10</span>
          </div>
          <div class="clip-desc">${{clip.description}}</div>
          <div style="font-size:0.75rem; color:var(--text-muted); margin-top:4px;">
            ⏱ ${{minutes}}:${{seconds}} - ${{Math.floor(clip.end/60)}}:${{Math.floor(clip.end%60).toString().padStart(2, '0')}} · ${{clip.video_name}}
          </div>
        `;
        container.appendChild(card);
      }});
    }}

    function playClip(idx) {{
      if (idx < 0 || idx >= clips.length) return;
      currentIndex = idx;
      const clip = clips[idx];

      renderPlaylist();

      const src = clip.video_path.startsWith('/') ? 'file://' + clip.video_path : clip.video_path;
      if (video.src !== src) {{
        video.src = src;
      }}
      video.currentTime = clip.start;
      video.play();
    }}

    video.addEventListener('timeupdate', () => {{
      const clip = clips[currentIndex];
      if (clip && autoNextCheckbox.checked && video.currentTime >= clip.end) {{
        if (currentIndex + 1 < clips.length) {{
          playClip(currentIndex + 1);
        }}
      }}
    }});

    document.getElementById('playAllBtn').onclick = () => playClip(0);

    renderPlaylist();
    if (clips.length > 0) {{
      playClip(0);
    }}
  </script>
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return str(out_path)

"""RDX-MCP 的 report bundle 构建器。

从已完成的 :class:`~rdx.models.TaskState` 生成自包含的 debug report bundle，
包含：

* ``report.json`` —— 机器可读的结构化报告，包含 RDX 规范的全部字段。
* ``report.md`` —— 人类可读的 Markdown 摘要。
* ``index.html`` —— 单文件交互式 HTML viewer（dark theme、event tree、
  image comparison、pipeline inspector、experiments timeline）。
* ``assets/`` —— 引用的 artifacts 副本（images、snapshots 等）。
* ``experiments/`` —— 单个 experiment 结果 JSON。

HTML template 以内嵌 Python 字符串常量形式存在，并通过 :mod:`jinja2`
渲染，确保输出为单个自包含文件，无外部依赖。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rdx.models import (
    ExperimentResult,
    PipelineSnapshot,
    ReportBundle,
    TaskState,
)

logger = logging.getLogger("rdx.core.report_builder")

# ---------------------------------------------------------------------------
# HTML template（内嵌 Jinja2）
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RDX Debug Report &mdash; {{ task_id }}</title>
<style>
/* ---- Reset & base ---- */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg-primary: #1a1b26;
  --bg-secondary: #24283b;
  --bg-tertiary: #292e42;
  --bg-hover: #343b58;
  --text-primary: #c0caf5;
  --text-secondary: #a9b1d6;
  --text-muted: #565f89;
  --accent-blue: #7aa2f7;
  --accent-cyan: #7dcfff;
  --accent-green: #9ece6a;
  --accent-yellow: #e0af68;
  --accent-orange: #ff9e64;
  --accent-red: #f7768e;
  --accent-magenta: #bb9af7;
  --border-color: #3b4261;
  --radius: 6px;
  --font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
html, body { height: 100%; font-family: var(--font-sans); background: var(--bg-primary); color: var(--text-primary); font-size: 14px; line-height: 1.5; }
a { color: var(--accent-blue); text-decoration: none; }
a:hover { text-decoration: underline; }
code, pre { font-family: var(--font-mono); font-size: 13px; }
pre { background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: var(--radius); padding: 12px 16px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }

/* ---- Layout grid ---- */
.app { display: grid; height: 100vh; grid-template-columns: 260px 1fr 320px; grid-template-rows: 48px 1fr 260px; grid-template-areas: "header header header" "sidebar viewer inspector" "sidebar timeline timeline"; }
.app-header { grid-area: header; background: var(--bg-secondary); border-bottom: 1px solid var(--border-color); display: flex; align-items: center; padding: 0 20px; gap: 16px; }
.app-header h1 { font-size: 15px; font-weight: 600; white-space: nowrap; }
.app-header .badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.badge-bug { background: var(--accent-red); color: var(--bg-primary); }
.badge-conf { background: var(--accent-green); color: var(--bg-primary); }

/* ---- Sidebar: event tree ---- */
.sidebar { grid-area: sidebar; background: var(--bg-secondary); border-right: 1px solid var(--border-color); overflow-y: auto; padding: 8px 0; }
.sidebar h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); padding: 8px 16px 4px; }
.tree-node { padding: 4px 12px 4px calc(12px + var(--depth, 0) * 16px); cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px; border-left: 3px solid transparent; transition: background 0.1s, border-color 0.1s; }
.tree-node:hover { background: var(--bg-hover); }
.tree-node.active { background: var(--bg-tertiary); border-left-color: var(--accent-blue); }
.tree-node .icon { width: 14px; height: 14px; display: inline-flex; align-items: center; justify-content: center; font-size: 10px; color: var(--text-muted); flex-shrink: 0; }
.tree-node .eid { color: var(--accent-cyan); font-family: var(--font-mono); font-size: 12px; min-width: 42px; }
.tree-node .name { color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tree-node.hotspot .eid { color: var(--accent-red); font-weight: 700; }
.tree-toggle { user-select: none; width: 16px; text-align: center; color: var(--text-muted); flex-shrink: 0; }
.tree-children { display: none; }
.tree-children.open { display: block; }

/* ---- Center: image viewer ---- */
.viewer { grid-area: viewer; background: var(--bg-primary); display: flex; flex-direction: column; overflow: hidden; }
.viewer-tabs { display: flex; gap: 2px; background: var(--bg-secondary); padding: 6px 12px 0; border-bottom: 1px solid var(--border-color); }
.viewer-tab { padding: 6px 16px; font-size: 12px; cursor: pointer; border-radius: var(--radius) var(--radius) 0 0; color: var(--text-muted); transition: background 0.15s, color 0.15s; }
.viewer-tab:hover { color: var(--text-secondary); background: var(--bg-tertiary); }
.viewer-tab.active { background: var(--bg-primary); color: var(--accent-blue); font-weight: 600; }
.viewer-content { flex: 1; display: flex; align-items: center; justify-content: center; position: relative; overflow: hidden; padding: 16px; }
.viewer-content img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: var(--radius); border: 1px solid var(--border-color); image-rendering: pixelated; }
.viewer-placeholder { color: var(--text-muted); font-size: 13px; text-align: center; }
.comparison-slider { position: absolute; inset: 0; display: none; }
.comparison-slider.active { display: flex; }
.comparison-slider .side { flex: 1; overflow: hidden; position: relative; }
.comparison-slider .divider { width: 3px; background: var(--accent-blue); cursor: col-resize; position: relative; z-index: 2; }
.comparison-slider .divider::after { content: ''; position: absolute; top: 50%; left: -8px; width: 19px; height: 32px; background: var(--accent-blue); border-radius: 4px; transform: translateY(-50%); }

/* ---- Right panel: inspector tabs ---- */
.inspector { grid-area: inspector; background: var(--bg-secondary); border-left: 1px solid var(--border-color); display: flex; flex-direction: column; overflow: hidden; }
.inspector-tabs { display: flex; gap: 2px; padding: 6px 8px 0; border-bottom: 1px solid var(--border-color); flex-shrink: 0; }
.inspector-tab { padding: 5px 12px; font-size: 11px; cursor: pointer; border-radius: var(--radius) var(--radius) 0 0; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; transition: background 0.15s, color 0.15s; }
.inspector-tab:hover { color: var(--text-secondary); background: var(--bg-tertiary); }
.inspector-tab.active { background: var(--bg-tertiary); color: var(--accent-cyan); font-weight: 600; }
.inspector-panel { flex: 1; overflow-y: auto; padding: 12px; display: none; }
.inspector-panel.active { display: block; }
.inspector-panel h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 8px; }
.inspector-panel .kv-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--bg-primary); font-size: 13px; }
.inspector-panel .kv-key { color: var(--text-muted); }
.inspector-panel .kv-val { color: var(--text-primary); font-family: var(--font-mono); font-size: 12px; text-align: right; max-width: 60%; overflow: hidden; text-overflow: ellipsis; }
.json-tree { font-family: var(--font-mono); font-size: 12px; }
.json-key { color: var(--accent-cyan); }
.json-string { color: var(--accent-green); }
.json-number { color: var(--accent-orange); }
.json-bool { color: var(--accent-magenta); }
.json-null { color: var(--text-muted); }

/* ---- Bottom: experiments timeline ---- */
.timeline { grid-area: timeline; background: var(--bg-secondary); border-top: 1px solid var(--border-color); display: flex; flex-direction: column; overflow: hidden; }
.timeline h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); padding: 10px 16px 6px; flex-shrink: 0; }
.timeline-scroll { flex: 1; overflow-x: auto; overflow-y: hidden; padding: 0 16px 12px; display: flex; gap: 12px; align-items: stretch; }
.exp-card { min-width: 220px; max-width: 280px; background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: var(--radius); padding: 12px; display: flex; flex-direction: column; gap: 6px; flex-shrink: 0; transition: border-color 0.15s; }
.exp-card:hover { border-color: var(--accent-blue); }
.exp-card .exp-id { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); }
.exp-card .exp-verdict { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 4px; display: inline-block; width: fit-content; }
.verdict-fixed { background: rgba(158, 206, 106, 0.2); color: var(--accent-green); }
.verdict-improved { background: rgba(224, 175, 104, 0.2); color: var(--accent-yellow); }
.verdict-rejected { background: rgba(247, 118, 142, 0.2); color: var(--accent-red); }
.verdict-inconclusive { background: rgba(86, 95, 137, 0.3); color: var(--text-muted); }
.verdict-error { background: rgba(247, 118, 142, 0.15); color: var(--accent-red); }
.exp-card .exp-metrics { font-size: 12px; display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; }
.exp-card .exp-metrics .label { color: var(--text-muted); }
.exp-card .exp-metrics .val { color: var(--text-primary); font-family: var(--font-mono); font-size: 11px; }
.exp-connector { display: flex; align-items: center; color: var(--border-color); font-size: 20px; flex-shrink: 0; }

/* ---- Fix candidate banner ---- */
.fix-banner { background: rgba(158, 206, 106, 0.1); border: 1px solid var(--accent-green); border-radius: var(--radius); padding: 10px 16px; margin: 8px 12px; }
.fix-banner h4 { color: var(--accent-green); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
.fix-banner p { font-size: 13px; color: var(--text-secondary); }

/* ---- Summary section (inside inspector) ---- */
.summary-section { margin-bottom: 16px; }
.summary-section h3 { margin-bottom: 6px; }
.summary-list { list-style: none; }
.summary-list li { padding: 3px 0; font-size: 13px; color: var(--text-secondary); }
.summary-list li strong { color: var(--text-primary); }

/* ---- Scrollbar styling ---- */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg-primary); }
::-webkit-scrollbar-thumb { background: var(--bg-hover); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* ---- No-data state ---- */
.empty-state { display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 13px; height: 100%; padding: 20px; text-align: center; }
</style>
</head>
<body>
<div class="app">

  <!-- Header -->
  <header class="app-header">
    <h1>RDX Debug Report</h1>
    <span class="badge badge-bug">{{ bug_type }}</span>
    <span class="badge badge-conf" style="background: {{ confidence_color }};">{{ confidence_pct }}% confidence</span>
    <span style="flex:1;"></span>
    <span style="color: var(--text-muted); font-size: 12px;">Task {{ task_id }} &bull; {{ created_at }}</span>
  </header>

  <!-- Sidebar: event tree -->
  <nav class="sidebar" id="sidebar">
    <h2>Event Path</h2>
    <div id="event-tree">
      {% for node in event_path %}
      <div class="tree-node{% if node.is_bad %} hotspot{% endif %}"
           style="--depth: {{ node.depth }};"
           data-eid="{{ node.event_id }}">
        <span class="eid">{{ node.event_id }}</span>
        <span class="name">{{ node.name }}</span>
      </div>
      {% endfor %}
      {% if not event_path %}
      <div class="empty-state">No event path recorded</div>
      {% endif %}
    </div>
  </nav>

  <!-- Center: image viewer -->
  <section class="viewer">
    <div class="viewer-tabs" id="viewer-tabs">
      <div class="viewer-tab active" data-panel="final">Final</div>
      <div class="viewer-tab" data-panel="mask">Mask</div>
      <div class="viewer-tab" data-panel="diff">Diff</div>
      <div class="viewer-tab" data-panel="compare">Compare</div>
    </div>
    <div class="viewer-content" id="viewer-content">
      {% if assets.final_image %}
      <img id="img-final" src="assets/{{ assets.final_image }}" alt="Final render">
      {% else %}
      <div class="viewer-placeholder">No render artifacts available.<br>Run a debug session to generate images.</div>
      {% endif %}
      <img id="img-mask" src="{% if assets.mask_image %}assets/{{ assets.mask_image }}{% endif %}" alt="Anomaly mask" style="display:none;">
      <img id="img-diff" src="{% if assets.diff_image %}assets/{{ assets.diff_image }}{% endif %}" alt="Diff image" style="display:none;">
      <div class="comparison-slider" id="comparison-slider">
        <div class="side" id="compare-left">
          {% if assets.final_image %}<img src="assets/{{ assets.final_image }}" style="width:100%;height:100%;object-fit:contain;">{% endif %}
        </div>
        <div class="divider" id="compare-divider"></div>
        <div class="side" id="compare-right">
          {% if assets.mask_image %}<img src="assets/{{ assets.mask_image }}" style="width:100%;height:100%;object-fit:contain;">{% endif %}
        </div>
      </div>
    </div>
  </section>

  <!-- Right panel: inspector -->
  <aside class="inspector">
    <div class="inspector-tabs" id="inspector-tabs">
      <div class="inspector-tab active" data-panel="summary">Summary</div>
      <div class="inspector-tab" data-panel="pipeline">Pipeline</div>
      <div class="inspector-tab" data-panel="shaders">Shaders</div>
      <div class="inspector-tab" data-panel="resources">Resources</div>
    </div>

    <!-- Summary -->
    <div class="inspector-panel active" id="panel-summary">
      <div class="summary-section">
        <h3>Anomaly</h3>
        <div class="summary-list">
          {% if bbox %}
          <li>Bounding box: <strong>({{ bbox.x0 }}, {{ bbox.y0 }}) &ndash; ({{ bbox.x1 }}, {{ bbox.y1 }})</strong></li>
          {% endif %}
          {% for a in anomalies %}
          <li>{{ a.type }}: <strong>{{ a.nan_count }} NaN, {{ a.inf_count }} Inf</strong> (density {{ "%.4f"|format(a.density) }})</li>
          {% endfor %}
          {% if not anomalies and not bbox %}
          <li style="color: var(--text-muted);">No anomalies detected</li>
          {% endif %}
        </div>
      </div>
      <div class="summary-section">
        <h3>Bisect Result</h3>
        <div class="kv-row"><span class="kv-key">First bad event</span><span class="kv-val">{{ first_bad_event_id if first_bad_event_id is not none else 'N/A' }}</span></div>
        <div class="kv-row"><span class="kv-key">First good event</span><span class="kv-val">{{ first_good_event_id if first_good_event_id is not none else 'N/A' }}</span></div>
        <div class="kv-row"><span class="kv-key">Confidence</span><span class="kv-val">{{ confidence_pct }}%</span></div>
      </div>
      <div class="summary-section">
        <h3>Verifier Metrics</h3>
        {% for k, v in verifier_metrics.items() %}
        <div class="kv-row"><span class="kv-key">{{ k }}</span><span class="kv-val">{{ v }}</span></div>
        {% endfor %}
        {% if not verifier_metrics %}
        <div style="color: var(--text-muted); font-size: 13px;">No metrics recorded</div>
        {% endif %}
      </div>
      {% if fix_candidate %}
      <div class="fix-banner">
        <h4>Fix Candidate</h4>
        <p>{{ fix_candidate.get('description', 'No description') }}</p>
        {% if fix_candidate.get('patch_id') %}
        <p style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); margin-top: 4px;">patch: {{ fix_candidate.patch_id }}</p>
        {% endif %}
      </div>
      {% endif %}
    </div>

    <!-- Pipeline -->
    <div class="inspector-panel" id="panel-pipeline">
      {% if pipeline %}
      <h3>Render Targets</h3>
      {% for rt in pipeline.render_targets %}
      <div class="kv-row"><span class="kv-key">RT{{ loop.index0 }}</span><span class="kv-val">{{ rt.format }} {{ rt.width }}x{{ rt.height }}</span></div>
      {% endfor %}
      <h3 style="margin-top: 12px;">Depth / Stencil</h3>
      <div class="kv-row"><span class="kv-key">Depth test</span><span class="kv-val">{{ pipeline.depth_stencil.depth_test_enabled }}</span></div>
      <div class="kv-row"><span class="kv-key">Depth write</span><span class="kv-val">{{ pipeline.depth_stencil.depth_write_enabled }}</span></div>
      <div class="kv-row"><span class="kv-key">Depth func</span><span class="kv-val">{{ pipeline.depth_stencil.depth_func }}</span></div>
      <h3 style="margin-top: 12px;">Blend States</h3>
      {% for bs in pipeline.blend_states %}
      <div class="kv-row"><span class="kv-key">MRT{{ loop.index0 }}</span><span class="kv-val">{{ bs.src_color }} {{ bs.color_op }} {{ bs.dst_color }}{% if bs.enabled %} (on){% else %} (off){% endif %}</span></div>
      {% endfor %}
      <h3 style="margin-top: 12px;">Viewport / Topology</h3>
      <div class="kv-row"><span class="kv-key">Topology</span><span class="kv-val">{{ pipeline.topology }}</span></div>
      {% for k, v in pipeline.viewport.items() %}
      <div class="kv-row"><span class="kv-key">{{ k }}</span><span class="kv-val">{{ v }}</span></div>
      {% endfor %}
      {% else %}
      <div class="empty-state">No pipeline snapshot available</div>
      {% endif %}
    </div>

    <!-- Shaders -->
    <div class="inspector-panel" id="panel-shaders">
      {% if pipeline and pipeline.shaders %}
      {% for sh in pipeline.shaders %}
      <div style="margin-bottom: 12px; padding: 8px; background: var(--bg-primary); border-radius: var(--radius);">
        <div style="display: flex; justify-content: space-between; margin-bottom: 4px;">
          <span style="color: var(--accent-magenta); font-weight: 600; text-transform: uppercase;">{{ sh.stage }}</span>
          <span style="color: var(--text-muted); font-family: var(--font-mono); font-size: 11px;">{{ sh.encoding }}</span>
        </div>
        <div class="kv-row"><span class="kv-key">Entry</span><span class="kv-val">{{ sh.entry_point }}</span></div>
        <div class="kv-row"><span class="kv-key">Resource</span><span class="kv-val">{{ sh.resource_id }}</span></div>
        <div class="kv-row"><span class="kv-key">Hash</span><span class="kv-val" title="{{ sh.hash }}">{{ sh.hash[:16] }}...</span></div>
      </div>
      {% endfor %}
      {% else %}
      <div class="empty-state">No shader information available</div>
      {% endif %}
    </div>

    <!-- Resources -->
    <div class="inspector-panel" id="panel-resources">
      {% if pipeline and pipeline.bindings %}
      <table style="width:100%; font-size: 12px; border-collapse: collapse;">
        <thead>
          <tr style="color: var(--text-muted); text-align: left; border-bottom: 1px solid var(--border-color);">
            <th style="padding: 4px 6px;">Set</th>
            <th style="padding: 4px 6px;">Bind</th>
            <th style="padding: 4px 6px;">Type</th>
            <th style="padding: 4px 6px;">Name</th>
          </tr>
        </thead>
        <tbody>
          {% for b in pipeline.bindings %}
          <tr style="border-bottom: 1px solid var(--bg-primary);">
            <td style="padding: 3px 6px; font-family: var(--font-mono);">{{ b.set_or_space }}</td>
            <td style="padding: 3px 6px; font-family: var(--font-mono);">{{ b.binding }}</td>
            <td style="padding: 3px 6px; color: var(--accent-yellow);">{{ b.type }}</td>
            <td style="padding: 3px 6px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 120px;">{{ b.resource_name or b.resource_id }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty-state">No resource bindings available</div>
      {% endif %}
    </div>
  </aside>

  <!-- Bottom: experiments timeline -->
  <section class="timeline">
    <h2>Experiments Timeline</h2>
    <div class="timeline-scroll" id="timeline-scroll">
      {% for exp in experiments %}
      {% if not loop.first %}
      <div class="exp-connector">&rarr;</div>
      {% endif %}
      <div class="exp-card">
        <span class="exp-id">{{ exp.experiment_id }}</span>
        <span class="exp-verdict verdict-{{ exp.verdict_class }}">{{ exp.verdict }}</span>
        <div class="exp-metrics">
          <span class="label">Duration</span><span class="val">{{ "%.2f"|format(exp.duration_seconds) }}s</span>
          {% for mk, mv in exp.metric_deltas.items() %}
          <span class="label">{{ mk }}</span><span class="val">{{ mv }}</span>
          {% endfor %}
        </div>
        {% if exp.notes %}
        <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">{{ exp.notes }}</div>
        {% endif %}
      </div>
      {% endfor %}
      {% if not experiments %}
      <div class="empty-state" style="width:100%;">No experiments have been run yet</div>
      {% endif %}
    </div>
  </section>

</div>

<script>
/* ---- Viewer tab switching ---- */
(function() {
  var viewerTabs = document.querySelectorAll('#viewer-tabs .viewer-tab');
  var images = {
    final: document.getElementById('img-final'),
    mask: document.getElementById('img-mask'),
    diff: document.getElementById('img-diff')
  };
  var slider = document.getElementById('comparison-slider');

  viewerTabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      viewerTabs.forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      var panel = tab.dataset.panel;
      Object.keys(images).forEach(function(k) {
        if (images[k]) images[k].style.display = (k === panel) ? 'block' : 'none';
      });
      if (slider) slider.classList.toggle('active', panel === 'compare');
    });
  });
})();

/* ---- Inspector tab switching ---- */
(function() {
  var tabs = document.querySelectorAll('#inspector-tabs .inspector-tab');
  tabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      tabs.forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      document.querySelectorAll('.inspector-panel').forEach(function(p) {
        p.classList.remove('active');
      });
      var panel = document.getElementById('panel-' + tab.dataset.panel);
      if (panel) panel.classList.add('active');
    });
  });
})();

/* ---- Comparison slider drag ---- */
(function() {
  var divider = document.getElementById('compare-divider');
  var slider = document.getElementById('comparison-slider');
  var left = document.getElementById('compare-left');
  if (!divider || !slider || !left) return;
  var dragging = false;
  divider.addEventListener('mousedown', function(e) { dragging = true; e.preventDefault(); });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var rect = slider.getBoundingClientRect();
    var pct = Math.max(5, Math.min(95, ((e.clientX - rect.left) / rect.width) * 100));
    left.style.flex = '0 0 ' + pct + '%';
  });
  document.addEventListener('mouseup', function() { dragging = false; });
})();

/* ---- Event tree node click (highlight) ---- */
(function() {
  document.querySelectorAll('.tree-node').forEach(function(node) {
    node.addEventListener('click', function() {
      document.querySelectorAll('.tree-node.active').forEach(function(n) { n.classList.remove('active'); });
      node.classList.add('active');
    });
  });
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# ReportBuilder
# ---------------------------------------------------------------------------


class ReportBuilder:
    """从 task state 生成自包含的 debug report bundle。

    构建器无状态：所有数据通过方法参数传入，可安全复用于多次 report 生成。
    """

    # ------------------------------------------------------------------
    # build_bundle (public entry point)
    # ------------------------------------------------------------------

    async def build_bundle(
        self,
        task_state: TaskState,
        artifact_store: Any,
        output_dir: Path,
    ) -> Dict[str, Any]:
        """在磁盘上构建完整的 report bundle。

        在 *output_dir* 下创建如下目录结构::

            bundle/
              report.json
              report.md
              index.html
              assets/
                (copied artifact files)
              experiments/
                (experiment result JSONs)

        Parameters
        ----------
        task_state:
            已完成 debug session 的完整 :class:`~rdx.models.TaskState`。
        artifact_store:
            会话中使用的 :class:`~rdx.utils.artifact_store.ArtifactStore`
            实例（用于获取 artifact blobs）。
        output_dir:
            将创建 ``bundle/`` 目录的父路径。

        Returns
        -------
        dict
            Keys: ``bundle_path``, ``report_json_path``,
            ``report_html_path``, ``report_md_path``。
        """
        bundle_dir = Path(output_dir) / "bundle"
        assets_dir = bundle_dir / "assets"
        experiments_dir = bundle_dir / "experiments"

        # 创建目录结构。
        for d in (bundle_dir, assets_dir, experiments_dir):
            d.mkdir(parents=True, exist_ok=True)

        # -- 拷贝 artifacts ------------------------------------------------
        asset_mapping = await self._copy_artifacts(
            task_state, artifact_store, assets_dir,
        )

        # -- Generate report JSON ------------------------------------------
        report_data = self._generate_json(task_state)
        report_json_path = bundle_dir / "report.json"
        report_json_path.write_text(
            json.dumps(report_data, indent=2, default=str),
            encoding="utf-8",
        )

        # -- Write per-experiment JSONs ------------------------------------
        for exp in task_state.experiments:
            exp_path = experiments_dir / f"{exp.experiment_id}.json"
            exp_path.write_text(
                json.dumps(exp.model_dump(mode="json"), indent=2, default=str),
                encoding="utf-8",
            )

        # -- Generate Markdown report --------------------------------------
        markdown_text = self._generate_markdown(task_state, report_data)
        report_md_path = bundle_dir / "report.md"
        report_md_path.write_text(markdown_text, encoding="utf-8")

        # -- Generate HTML report ------------------------------------------
        html_text = self._render_html(
            task_state, report_data, asset_mapping,
        )
        report_html_path = bundle_dir / "index.html"
        report_html_path.write_text(html_text, encoding="utf-8")

        logger.info(
            "Report bundle written to %s (%d artifacts, %d experiments)",
            bundle_dir,
            len(asset_mapping),
            len(task_state.experiments),
        )

        return {
            "bundle_path": str(bundle_dir),
            "report_json_path": str(report_json_path),
            "report_html_path": str(report_html_path),
            "report_md_path": str(report_md_path),
        }

    # ------------------------------------------------------------------
    # _generate_json
    # ------------------------------------------------------------------

    def _generate_json(self, task_state: TaskState) -> Dict[str, Any]:
        """从 task state 构建完整的 ``report.json`` 结构。

        包含 RDX report schema 的全部字段：
        task_id、bugType、capture info、bisect boundaries、bounding box、
        verifier metrics、hypotheses、fix candidate、confidence、
        evidence artifacts、pipeline snapshot 以及 experiments。
        """
        # 确定 primary bug type。
        bug_types = []
        for hyp in task_state.hypotheses:
            bug_types.extend(bt.value for bt in hyp.bug_types)
        if not bug_types:
            # 从输入提示中推断。
            bug_types = [bt.value for bt in task_state.input.bug_type_hints]
        if not bug_types:
            bug_types = ["unknown"]
        primary_bug_type = bug_types[0]

        # Bisect 信息。
        bisect = task_state.bisect_result
        first_bad = bisect.first_bad_event_id if bisect else None
        first_good = bisect.first_good_event_id if bisect else None
        confidence = bisect.confidence if bisect else 0.0

        # 从第一个 anomaly 提取 bounding box。
        bbox_data: Optional[Dict[str, int]] = None
        if task_state.anomalies:
            bbox_obj = task_state.anomalies[0].bbox
            if bbox_obj is not None:
                bbox_data = {
                    "x0": bbox_obj.x0,
                    "y0": bbox_obj.y0,
                    "x1": bbox_obj.x1,
                    "y1": bbox_obj.y1,
                }

        # Verifier metrics: aggregate from experiment evidence.
        verifier_metrics: Dict[str, Any] = {}
        for exp in task_state.experiments:
            if exp.evidence is not None:
                if exp.evidence.before_metrics:
                    for k, v in exp.evidence.before_metrics.items():
                        verifier_metrics.setdefault(f"before.{k}", v)
                if exp.evidence.after_metrics:
                    for k, v in exp.evidence.after_metrics.items():
                        verifier_metrics.setdefault(f"after.{k}", v)

        # Hypotheses.
        hypotheses_data = []
        for hyp in task_state.hypotheses:
            hypotheses_data.append({
                "hypothesis_id": hyp.hypothesis_id,
                "title": hyp.title,
                "description": hyp.description,
                "bug_types": [bt.value for bt in hyp.bug_types],
                "proposed_patches": hyp.proposed_patches,
                "priority_score": hyp.priority_score,
                "result": hyp.result.value if hyp.result else None,
            })

        # Fix candidate: best hypothesis that yielded FIXED or IMPROVED.
        fix_candidate: Optional[Dict[str, Any]] = None
        for hyp in task_state.hypotheses:
            if hyp.result is not None and hyp.result.value in ("fixed", "improved"):
                fix_candidate = {
                    "hypothesis_id": hyp.hypothesis_id,
                    "title": hyp.title,
                    "description": hyp.description,
                    "result": hyp.result.value,
                    "patches": hyp.proposed_patches,
                }
                break

        # Evidence artifacts mapping.
        evidence_artifacts: Dict[str, str] = {}
        for exp in task_state.experiments:
            if exp.evidence is not None:
                if exp.evidence.before_artifact is not None:
                    evidence_artifacts[
                        f"{exp.experiment_id}_before"
                    ] = exp.evidence.before_artifact.uri
                if exp.evidence.after_artifact is not None:
                    evidence_artifacts[
                        f"{exp.experiment_id}_after"
                    ] = exp.evidence.after_artifact.uri

        # Pipeline snapshot.
        pipeline_data: Optional[Dict[str, Any]] = None
        if task_state.pipeline is not None:
            pipeline_data = task_state.pipeline.model_dump(mode="json")

        # Experiments.
        experiments_data = []
        for exp in task_state.experiments:
            experiments_data.append(
                exp.model_dump(mode="json"),
            )

        # Capture info.
        capture_data: Optional[Dict[str, Any]] = None
        if task_state.capture_id is not None:
            capture_data = {
                "capture_id": task_state.capture_id,
                "session_id": task_state.session_id,
                "rdc_path": task_state.input.rdc_path,
            }

        return {
            "task_id": task_state.task_id,
            "bugType": primary_bug_type,
            "bugTypes": list(set(bug_types)),
            "captureInfo": capture_data,
            "firstBadEventId": first_bad,
            "firstGoodEventId": first_good,
            "bbox": bbox_data,
            "verifierMetrics": verifier_metrics,
            "hypothesesTried": hypotheses_data,
            "fixCandidate": fix_candidate,
            "confidence": confidence,
            "evidence": {
                "event_path": self._build_event_path(task_state),
                "artifacts": evidence_artifacts,
            },
            "pipelineSnapshot": pipeline_data,
            "experiments": experiments_data,
            "anomalies": [
                a.model_dump(mode="json") for a in task_state.anomalies
            ],
            "status": task_state.status,
            "createdAt": task_state.created_at,
            "updatedAt": task_state.updated_at,
        }

    # ------------------------------------------------------------------
    # _generate_markdown
    # ------------------------------------------------------------------

    def _generate_markdown(
        self,
        task_state: TaskState,
        report_data: Dict[str, Any],
    ) -> str:
        """生成可读的 Markdown 调试摘要。"""
        lines: List[str] = []
        confidence_pct = round(report_data.get("confidence", 0) * 100, 1)
        bug_type = report_data.get("bugType", "unknown")

        # -- Title（标题）------------------------------------------------
        lines.append(
            f"# RDX Debug Report: {bug_type.upper()} "
            f"({confidence_pct}% confidence)"
        )
        lines.append("")
        lines.append(f"**Task ID:** `{task_state.task_id}`  ")
        lines.append(f"**Status:** {task_state.status}  ")
        lines.append(
            f"**Capture:** `{task_state.input.rdc_path}`  "
        )
        lines.append("")

        # -- Summary（摘要）-----------------------------------------------
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- **Bug type:** {bug_type}")

        first_bad = report_data.get("firstBadEventId")
        first_good = report_data.get("firstGoodEventId")
        if first_bad is not None:
            lines.append(
                f"- **First bad event:** {first_bad} "
                f"(last good: {first_good})"
            )
        lines.append(
            f"- **Confidence:** {confidence_pct}%"
        )
        lines.append(
            f"- **Hypotheses tried:** {len(task_state.hypotheses)}"
        )
        lines.append(
            f"- **Experiments run:** {len(task_state.experiments)}"
        )
        lines.append("")

        # -- Anomaly details -----------------------------------------------
        if task_state.anomalies:
            lines.append("## Anomaly Details")
            lines.append("")
            for i, anom in enumerate(task_state.anomalies):
                lines.append(f"### Anomaly {i + 1}: {anom.type}")
                if anom.bbox:
                    lines.append(
                        f"- **Bounding box:** "
                        f"({anom.bbox.x0}, {anom.bbox.y0}) to "
                        f"({anom.bbox.x1}, {anom.bbox.y1})"
                    )
                lines.append(f"- **NaN count:** {anom.nan_count}")
                lines.append(f"- **Inf count:** {anom.inf_count}")
                lines.append(f"- **Total pixels:** {anom.total_pixels}")
                lines.append(
                    f"- **Density:** {anom.density:.6f}"
                )
                lines.append("")

        # -- Pipeline / Shader summary -------------------------------------
        if task_state.pipeline:
            pipe = task_state.pipeline
            lines.append("## Pipeline State")
            lines.append("")
            lines.append(f"- **API:** {pipe.api.value}")
            lines.append(f"- **Topology:** {pipe.topology}")
            lines.append(
                f"- **Render targets:** {len(pipe.render_targets)}"
            )
            if pipe.shaders:
                lines.append("")
                lines.append("### Shaders")
                lines.append("")
                lines.append("| Stage | Entry Point | Encoding | Hash |")
                lines.append("|-------|-------------|----------|------|")
                for sh in pipe.shaders:
                    short_hash = sh.hash[:16] + "..." if sh.hash else "N/A"
                    lines.append(
                        f"| {sh.stage.value.upper()} | "
                        f"{sh.entry_point} | "
                        f"{sh.encoding} | "
                        f"`{short_hash}` |"
                    )
            lines.append("")

        # -- Experiments timeline ------------------------------------------
        if task_state.experiments:
            lines.append("## Experiments")
            lines.append("")
            for i, exp in enumerate(task_state.experiments, 1):
                verdict = "N/A"
                notes = ""
                if exp.evidence:
                    verdict = exp.evidence.verdict.value
                    notes = exp.evidence.notes or ""
                lines.append(
                    f"### {i}. {exp.experiment_id}"
                )
                lines.append(
                    f"- **Status:** {exp.status.value}"
                )
                lines.append(
                    f"- **Verdict:** {verdict}"
                )
                lines.append(
                    f"- **Duration:** {exp.duration_seconds:.2f}s"
                )
                if notes:
                    lines.append(f"- **Notes:** {notes}")
                lines.append("")

        # -- Fix candidate -------------------------------------------------
        fix_cand = report_data.get("fixCandidate")
        if fix_cand:
            lines.append("## Fix Candidate")
            lines.append("")
            lines.append(
                f"**{fix_cand.get('title', 'Untitled')}** "
                f"({fix_cand.get('result', 'N/A')})"
            )
            desc = fix_cand.get("description", "")
            if desc:
                lines.append("")
                lines.append(desc)
            patches = fix_cand.get("patches", [])
            if patches:
                lines.append("")
                lines.append("Proposed patches:")
                for pid in patches:
                    lines.append(f"- `{pid}`")
            lines.append("")

        # -- Footer --------------------------------------------------------
        lines.append("---")
        lines.append(
            f"*Generated by RDX-MCP at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(task_state.updated_at))}*"
        )
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # _render_html
    # ------------------------------------------------------------------

    def _render_html(
        self,
        task_state: TaskState,
        report_data: Dict[str, Any],
        asset_mapping: Dict[str, str],
    ) -> str:
        """使用内嵌模板渲染交互式 HTML report。

        优先使用 Jinja2；若未安装 Jinja2，则对关键字段进行基础字符串替换。
        """
        confidence = report_data.get("confidence", 0)
        confidence_pct = round(confidence * 100, 1)
        bug_type = report_data.get("bugType", "unknown")

        # Confidence badge 颜色。
        if confidence >= 0.8:
            confidence_color = "var(--accent-green)"
        elif confidence >= 0.5:
            confidence_color = "var(--accent-yellow)"
        else:
            confidence_color = "var(--accent-red)"

        # 构建侧边栏的 event path。
        event_path = self._build_event_path(task_state)
        first_bad = report_data.get("firstBadEventId")
        sidebar_nodes = []
        for ep in event_path:
            sidebar_nodes.append({
                "event_id": ep.get("event_id", 0),
                "name": ep.get("name", ""),
                "depth": ep.get("depth", 0),
                "is_bad": ep.get("event_id") == first_bad,
            })

        # 解析 viewer 使用的 asset 文件名。
        assets = {
            "final_image": asset_mapping.get("final_image", ""),
            "mask_image": asset_mapping.get("mask_image", ""),
            "diff_image": asset_mapping.get("diff_image", ""),
        }

        # 构建 experiment cards。
        exp_cards = []
        for exp in task_state.experiments:
            verdict = "N/A"
            verdict_class = "inconclusive"
            notes = ""
            metric_deltas: Dict[str, str] = {}

            if exp.evidence is not None:
                verdict = exp.evidence.verdict.value
                verdict_class = exp.evidence.verdict.value.lower().replace(" ", "")
                notes = exp.evidence.notes or ""

                # 构建 metric delta 字符串。
                before = exp.evidence.before_metrics or {}
                after = exp.evidence.after_metrics or {}
                all_keys = sorted(
                    set(before.keys()) | set(after.keys())
                )
                for mk in all_keys:
                    if mk in ("passed", "error"):
                        continue
                    bv = before.get(mk)
                    av = after.get(mk)
                    if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
                        metric_deltas[mk] = f"{bv} -> {av}"
                    elif bv is not None:
                        metric_deltas[mk] = str(bv)

            exp_cards.append({
                "experiment_id": exp.experiment_id,
                "verdict": verdict,
                "verdict_class": verdict_class,
                "duration_seconds": exp.duration_seconds,
                "notes": _truncate(notes, 120),
                "metric_deltas": metric_deltas,
            })

        # Anomalies for summary panel.
        anomalies_data = []
        for a in task_state.anomalies:
            anomalies_data.append({
                "type": a.type,
                "nan_count": a.nan_count,
                "inf_count": a.inf_count,
                "density": a.density,
            })

        # Bbox.
        bbox = None
        if task_state.anomalies and task_state.anomalies[0].bbox:
            b = task_state.anomalies[0].bbox
            bbox = {"x0": b.x0, "y0": b.y0, "x1": b.x1, "y1": b.y1}

        # Verifier metrics.
        verifier_metrics = report_data.get("verifierMetrics", {})

        # Fix candidate.
        fix_candidate = report_data.get("fixCandidate")

        # Pipeline.
        pipeline = task_state.pipeline

        # Timestamps.
        created_at = time.strftime(
            "%Y-%m-%d %H:%M UTC",
            time.gmtime(task_state.created_at),
        )

        # -- Render with Jinja2 --------------------------------------------
        try:
            import jinja2
        except ImportError:
            logger.warning(
                "jinja2 not installed; generating minimal HTML report"
            )
            return self._render_html_fallback(
                task_state, report_data, asset_mapping,
            )

        env = jinja2.Environment(
            autoescape=True,
            undefined=jinja2.SilentUndefined,
        )
        template = env.from_string(_HTML_TEMPLATE)

        return template.render(
            task_id=task_state.task_id,
            bug_type=bug_type,
            confidence_pct=confidence_pct,
            confidence_color=confidence_color,
            created_at=created_at,
            event_path=sidebar_nodes,
            assets=assets,
            experiments=exp_cards,
            anomalies=anomalies_data,
            bbox=bbox,
            first_bad_event_id=report_data.get("firstBadEventId"),
            first_good_event_id=report_data.get("firstGoodEventId"),
            verifier_metrics=verifier_metrics,
            fix_candidate=fix_candidate,
            pipeline=pipeline,
        )

    def _render_html_fallback(
        self,
        task_state: TaskState,
        report_data: Dict[str, Any],
        asset_mapping: Dict[str, str],
    ) -> str:
        """在不使用 Jinja2 的情况下生成最小 HTML report。

        仅在未安装 Jinja2 时使用。输出可用，但缺少模板版本的完整交互特性。
        """
        confidence_pct = round(
            report_data.get("confidence", 0) * 100, 1,
        )
        bug_type = report_data.get("bugType", "unknown")
        first_bad = report_data.get("firstBadEventId", "N/A")
        first_good = report_data.get("firstGoodEventId", "N/A")

        experiments_html_parts = []
        for exp in task_state.experiments:
            verdict = "N/A"
            if exp.evidence:
                verdict = exp.evidence.verdict.value
            experiments_html_parts.append(
                f'<div style="display:inline-block;margin:8px;padding:12px;'
                f'background:#292e42;border-radius:6px;min-width:200px;">'
                f'<div style="color:#565f89;font-size:11px;">{exp.experiment_id}</div>'
                f'<div style="font-weight:600;margin:4px 0;">{verdict}</div>'
                f'<div style="color:#a9b1d6;font-size:12px;">'
                f'{exp.duration_seconds:.2f}s</div></div>'
            )

        return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>RDX Debug Report - {task_state.task_id}</title>
<style>
body {{ font-family: -apple-system, sans-serif; background: #1a1b26; color: #c0caf5; padding: 40px; line-height: 1.6; }}
h1 {{ color: #7aa2f7; }} h2 {{ color: #7dcfff; margin-top: 24px; }}
.card {{ background: #24283b; border: 1px solid #3b4261; border-radius: 8px; padding: 20px; margin: 16px 0; }}
.kv {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #1a1b26; }}
.kv-key {{ color: #565f89; }} .kv-val {{ font-family: monospace; }}
</style>
</head>
<body>
<h1>RDX Debug Report</h1>
<p>Bug type: <strong>{bug_type}</strong> | Confidence: <strong>{confidence_pct}%</strong></p>
<p>Task: <code>{task_state.task_id}</code> | Status: {task_state.status}</p>

<div class="card">
<h2>Bisect Result</h2>
<div class="kv"><span class="kv-key">First bad event</span><span class="kv-val">{first_bad}</span></div>
<div class="kv"><span class="kv-key">First good event</span><span class="kv-val">{first_good}</span></div>
</div>

<div class="card">
<h2>Experiments ({len(task_state.experiments)})</h2>
<div style="overflow-x:auto;white-space:nowrap;">
{''.join(experiments_html_parts) if experiments_html_parts else '<p style="color:#565f89;">No experiments</p>'}
</div>
</div>

<pre>{json.dumps(report_data, indent=2, default=str)}</pre>
</body>
</html>
"""

    # ------------------------------------------------------------------
    # _copy_artifacts
    # ------------------------------------------------------------------

    async def _copy_artifacts(
        self,
        task_state: TaskState,
        artifact_store: Any,
        assets_dir: Path,
    ) -> Dict[str, str]:
        """将引用的 artifacts 拷贝到 ``assets/`` 目录。

        返回 logical name 到 assets_dir 内文件名的映射
        （例如 ``{"final_image": "render_evt42.png", ...}``）。
        """
        mapping: Dict[str, str] = {}

        # 收集 experiments 中的所有 artifact refs。
        artifact_refs: List[tuple] = []  # (logical_name, ArtifactRef)

        for i, exp in enumerate(task_state.experiments):
            if exp.evidence is None:
                continue
            if exp.evidence.before_artifact is not None:
                artifact_refs.append(
                    (f"exp{i}_before", exp.evidence.before_artifact),
                )
            if exp.evidence.after_artifact is not None:
                artifact_refs.append(
                    (f"exp{i}_after", exp.evidence.after_artifact),
                )

        # 收集 anomaly mask artifacts。
        for i, anom in enumerate(task_state.anomalies):
            if anom.mask_artifact is not None:
                artifact_refs.append(
                    (f"anomaly{i}_mask", anom.mask_artifact),
                )

        # 逐个拷贝 artifact。
        for logical_name, ref in artifact_refs:
            try:
                # 依据 MIME type 确定文件扩展名。
                ext = _mime_to_ext(ref.mime)
                dest_filename = f"{logical_name}{ext}"
                dest_path = assets_dir / dest_filename

                # 尝试从 artifact store 获取源路径。
                sha = ref.sha256
                if hasattr(artifact_store, "get_path"):
                    src_path = artifact_store.get_path(sha)
                    if src_path.is_file():
                        shutil.copy2(str(src_path), str(dest_path))
                        mapping[logical_name] = dest_filename
                        continue

                # 回退：直接读取字节并写入。
                if hasattr(artifact_store, "retrieve"):
                    data = await artifact_store.retrieve(sha)
                    dest_path.write_bytes(data)
                    mapping[logical_name] = dest_filename
                    continue

                logger.warning(
                    "Cannot copy artifact %s: store has no get_path or "
                    "retrieve method",
                    logical_name,
                )

            except Exception as exc:
                logger.warning(
                    "Failed to copy artifact %s (%s): %s",
                    logical_name, ref.uri, exc,
                )

        # Map well-known logical names for the HTML viewer.
        # Use the first experiment's before image as "final_image",
        # and the first anomaly mask as "mask_image".
        if "exp0_before" in mapping and "final_image" not in mapping:
            mapping["final_image"] = mapping["exp0_before"]
        if "exp0_after" in mapping and "diff_image" not in mapping:
            mapping["diff_image"] = mapping["exp0_after"]
        if "anomaly0_mask" in mapping and "mask_image" not in mapping:
            mapping["mask_image"] = mapping["anomaly0_mask"]

        logger.debug(
            "Copied %d artifacts to %s", len(mapping), assets_dir,
        )
        return mapping

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_event_path(task_state: TaskState) -> List[Dict[str, Any]]:
        """构建用于侧边栏的扁平 event-path 列表。

        从 bisect 结果的 evidence chain 以及任务中的 anomalies / experiments
        提取关键事件，形成可导航的重要事件时间线。
        """
        events: List[Dict[str, Any]] = []
        seen: set = set()

        # Bisect 边界事件。
        if task_state.bisect_result is not None:
            br = task_state.bisect_result
            if br.first_good_event_id not in seen:
                events.append({
                    "event_id": br.first_good_event_id,
                    "name": "Last good event",
                    "depth": 0,
                })
                seen.add(br.first_good_event_id)
            if br.first_bad_event_id not in seen:
                events.append({
                    "event_id": br.first_bad_event_id,
                    "name": "First bad event",
                    "depth": 0,
                })
                seen.add(br.first_bad_event_id)

        # Pipeline 事件。
        if task_state.pipeline is not None:
            eid = task_state.pipeline.event_id
            if eid not in seen:
                events.append({
                    "event_id": eid,
                    "name": "Pipeline snapshot",
                    "depth": 1,
                })
                seen.add(eid)

        # Experiment events.
        for exp in task_state.experiments:
            # The ExperimentResult itself does not carry event_id, but
            # the experiment definition (which lives on the evidence)
            # originally targeted an event.  We use experiment_id as a
            # placeholder label.
            exp_eid_str = exp.experiment_id
            if exp.evidence is not None:
                verdict = exp.evidence.verdict.value
            else:
                verdict = "unknown"
            # We cannot recover the event_id from ExperimentResult alone,
            # so we use experiment ordering as a proxy.
            exp_index = len(events)
            events.append({
                "event_id": exp_index,
                "name": f"Experiment: {verdict} ({exp_eid_str})",
                "depth": 1,
            })

        return events


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------


def _mime_to_ext(mime: str) -> str:
    """将 MIME type 映射为文件扩展名。"""
    table: Dict[str, str] = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/x-exr": ".exr",
        "image/vnd.radiance": ".hdr",
        "application/x-npz": ".npz",
        "application/json": ".json",
        "application/octet-stream": ".bin",
    }
    return table.get(mime.lower(), ".bin")


def _truncate(text: str, max_len: int) -> str:
    """将 *text* 截断为 *max_len* 字符，必要时添加省略号。"""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."

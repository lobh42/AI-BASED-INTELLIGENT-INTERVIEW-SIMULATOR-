"""Real-time Voice Interview powered by Vapi Web SDK.

Architecture note
-----------------
Vapi uses Daily.co for WebRTC under the hood. Daily.co calls postMessage() to
communicate between its nested iframe and the parent window.  Browsers block
postMessage when the parent origin is 'null' — which is exactly what Streamlit's
components.html() produces (it renders via <iframe srcdoc="...">, giving null origin).

Fix: spin up a tiny local HTTP server (daemon thread) on port 8503 that serves
the Vapi HTML page with a real origin (http://localhost:8503), then embed it
via st.components.v1.iframe() pointing at that URL.
"""

import os
import threading
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
import database as db
from browser_lock import inject_browser_lock
from webcam_proctor import inject_webcam_proctor

# ── Bootstrap ────────────────────────────────────────────────────────────────
load_dotenv()

st.set_page_config(
    page_title="IntervueX - DSA Voice Interview",
    page_icon="💻",
    layout="wide",
)

import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ui_utils import apply_global_css
apply_global_css()

# ── Auth guard ───────────────────────────────────────────────────────────────
if not st.session_state.get("user_id"):
    st.warning("Please sign in from the home page to start a voice interview.")
    st.stop()

user_id = st.session_state.user_id

# ── Session State ────────────────────────────────────────────────────────────
if "voice_session_id" not in st.session_state:
    st.session_state.voice_session_id = None
if "voice_interview_active" not in st.session_state:
    st.session_state.voice_interview_active = False

# ── Vapi Configuration ───────────────────────────────────────────────────────
_raw = os.getenv("VAPI_PUBLIC_KEY", "") or ""
VAPI_PUBLIC_KEY = _raw.strip().strip('"').strip("'")
VAPI_PORT = 8503

VAPI_WIDGET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>IntervueX Voice - Alex</title>
<style>
  :root {
    --primary: #6366f1;
    --primary-glow: rgba(99, 102, 241, 0.5);
    --bg-dark: #0f172a;
    --glass: rgba(30, 41, 59, 0.7);
    --text-main: #f8fafc;
    --text-dim: #94a3b8;
  }
  
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    font-family:'Inter', system-ui, -apple-system, sans-serif;
    background: radial-gradient(circle at center, #1e293b 0%, #0f172a 100%);
    color: var(--text-main);
    min-height:100vh; display:flex; flex-direction:column;
    align-items:center; justify-content:center; gap:24px; padding:24px;
    overflow: hidden;
  }

  .container {
    width: 100%; max-width: 500px;
    background: var(--glass);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 24px;
    padding: 32px;
    text-align: center;
    box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
  }

  h2 { font-size:1.8rem; font-weight:800; background: linear-gradient(to right, #818cf8, #c084fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom:8px; }
  .subtitle { font-size:.9rem; color:var(--text-dim); margin-bottom:24px; }

  .orb-container {
    position: relative; width: 140px; height: 140px; margin: 0 auto 32px;
  }
  .orb {
    width: 100%; height: 100%;
    background: linear-gradient(135deg, #6366f1 0%, #a855f7 100%);
    border-radius: 50%;
    box-shadow: 0 0 40px var(--primary-glow);
    display: flex; align-items: center; justify-content: center;
    position: relative; z-index: 2;
  }
  .orb-pulse {
    position: absolute; top:0; left:0; width:100%; height:100%;
    background: var(--primary); border-radius: 50%;
    opacity: 0.5; z-index: 1;
    animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
  }
  @keyframes pulse {
    0% { transform: scale(1); opacity: 0.5; }
    100% { transform: scale(1.6); opacity: 0; }
  }

  .status-badge {
    display:inline-flex; align-items:center; gap:8px;
    padding:8px 16px; border-radius:99px;
    font-size:.85rem; font-weight:600; margin-bottom: 24px;
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #64748b; }
  .active .status-dot { background: #10b981; box-shadow: 0 0 10px #10b981; }

  .controls { display:flex; gap:16px; justify-content:center; }
  .btn {
    padding: 14px 28px; border-radius: 14px; border: none;
    font-size: 1rem; font-weight: 700; cursor: pointer;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    display: inline-flex; align-items: center; gap: 8px;
  }
  .btn-start { background: #6366f1; color: white; box-shadow: 0 4px 15px rgba(99, 102, 241, 0.4); }
  .btn-start:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 8px 25px rgba(99, 102, 241, 0.6); }
  .btn-stop { background: rgba(239, 68, 68, 0.1); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.2); }
  .btn-stop:hover:not(:disabled) { background: #ef4444; color: white; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }

  .transcript {
    margin-top: 32px; padding: 16px; background: rgba(0,0,0,0.2);
    border-radius: 16px; max-height: 120px; overflow-y: auto;
    font-size: 0.9rem; color: var(--text-dim); line-height: 1.5;
    text-align: left; display: none;
  }
  .transcript strong { color: #818cf8; }

  #error { color: #f87171; font-size: 0.85rem; margin-top: 16px; display: none; }
</style>
</head>
<body>

<div class="container">
  <h2>Alex AI Interviewer</h2>
  <p class="subtitle">AI Technical Recruiter &bull; Real-time Voice</p>

  <div class="orb-container">
    <div id="orbPulse" class="orb-pulse" style="display:none;"></div>
    <div class="orb">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
        <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
        <line x1="12" y1="19" x2="12" y2="23"></line>
        <line x1="8" y1="23" x2="16" y2="23"></line>
      </svg>
    </div>
  </div>

  <div id="statusBadge" class="status-badge">
    <div id="statusDot" class="status-dot"></div>
    <span id="statusText">Ready to start</span>
  </div>

  <div class="controls">
    <button id="startBtn" class="btn btn-start" onclick="startInterview()">
      Start Interview
    </button>
    <button id="stopBtn" class="btn btn-stop" onclick="stopInterview()" disabled>
      End Call
    </button>
  </div>

  <div id="transcript" class="transcript"></div>
  <div id="error"></div>
</div>

<script type="module">
  import Vapi from 'https://esm.sh/@vapi-ai/web@latest';

  const KEY = '%%VAPI_KEY%%';
  let vapi = null, active = false;

  const $ = id => document.getElementById(id);

  function updateStatus(text, mode) {
    $('statusText').textContent = text;
    $('statusBadge').className = 'status-badge ' + mode;
    $('orbPulse').style.display = (mode === 'active') ? 'block' : 'none';
  }

  async function startInterview() {
    if (!KEY) { $('error').textContent = 'API Key missing'; return; }
    $('startBtn').disabled = true;
    updateStatus('Connecting...', 'loading');

    try {
      vapi = new Vapi(KEY);

      vapi.on('call-start', () => {
        active = true;
        updateStatus('Live Interview', 'active');
        $('stopBtn').disabled = false;
        $('transcript').style.display = 'block';
      });

      vapi.on('call-end', () => {
        active = false;
        updateStatus('Ready to start', 'idle');
        $('startBtn').disabled = false;
        $('stopBtn').disabled = true;
      });

      vapi.on('message', msg => {
        if (msg.type === 'transcript') {
          const t = $('transcript');
          t.innerHTML = `<strong>${msg.role}:</strong> ${msg.transcript}`;
          t.scrollTop = t.scrollHeight;
        }
      });
      
      vapi.on('error', e => {
        console.error(e);
        $('error').textContent = 'Connection error. Please retry.';
        $('error').style.display = 'block';
        updateStatus('Error', 'error');
        $('startBtn').disabled = false;
      });

      await vapi.start({
        transcriber: { provider: 'deepgram', model: 'nova-2', language: 'en-US' },
        model: {
          provider: 'openai', model: 'gpt-4o-mini', temperature: 0.7,
          messages: [{
            role: 'system',
            content: `You are Alex, a senior technical interviewer at a top-tier tech company. 
            Conduct a structured, professional, and friendly interview.
            
            STRUCTURE:
            1. Brief intro (15s).
            2. Ask one DSA question at a time. Wait for answer. 
            3. Ask follow-up/clarification if needed.
            4. Move to next after candidate finishes.
            5. Wrap up after 3-4 questions total.
            
            GUIDELINES:
            - Keep responses under 40 words.
            - Be encouraging but maintain high standards.
            - Don't give answers immediately; guide the candidate.
            - If they get stuck, give a tiny hint.`
          }]
        },
        voice: { provider: '11labs', voiceId: '21m00Tcm4TlvDq8ikWAM' },
        name: 'Alex AI',
        firstMessage: "Hi there! I'm Alex. Ready to dive into some technical questions?"
      });
    } catch (e) {
      console.error(e);
      $('startBtn').disabled = false;
    }
  }

  function stopInterview() {
    if (vapi) vapi.stop();
  }

  window.startInterview = startInterview;
  window.stopInterview = stopInterview;
</script>
</body>
</html>
"""

def _make_vapi_html(key: str) -> bytes:
    return VAPI_WIDGET_HTML.replace("%%VAPI_KEY%%", key).encode("utf-8")

def _start_vapi_server(key: str, port: int) -> None:
    import http.server
    import socketserver
    html_bytes = _make_vapi_html(key)
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.send_header("X-Frame-Options", "ALLOWALL")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(html_bytes)
        def log_message(self, *_): pass
    
    # Use allow_reuse_address to avoid "Address already in use" errors on rerun
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("localhost", port), Handler) as httpd:
            httpd.serve_forever()
    except Exception:
        pass # Port likely busy

# Start server once
if "vapi_server_started" not in st.session_state:
    t = threading.Thread(target=_start_vapi_server, args=(VAPI_PUBLIC_KEY, VAPI_PORT), daemon=True)
    t.start()
    st.session_state.vapi_server_started = True

# ── Page Content ─────────────────────────────────────────────────────────────
st.markdown("## 💻 DSA Voice Interview")
st.markdown("*Talk to Alex — our interactive AI interviewer.*")
st.markdown("---")

if not st.session_state.voice_interview_active:
    # Setup / Start screen
    st.markdown("### Prepare for your Voice Interview")
    st.info("💡 Ensure your microphone is active. Alex will ask 3-4 questions.")
    
    if st.button("🚀 Start Personalized Session", type="primary"):
        # Create a database session to track violations and progress
        session_id = db.create_session(user_id=user_id, session_type="voice")
        st.session_state.voice_session_id = session_id
        st.session_state.voice_interview_active = True
        st.rerun()
else:
    # Active Interview Session
    session_id = st.session_state.voice_session_id
    
    # Inject browser lock and webcam proctoring
    inject_browser_lock(session_id)
    inject_webcam_proctor(session_id)
    
    # Session status bar
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        st.markdown(f"**Session #{session_id}**")
    with col2:
        session_data = db.get_session(session_id)
        violations = session_data.get("tab_violations", 0) if session_data else 0
        if violations > 0: st.error(f"⚠️ Violations: {violations}")
        else: st.success("✅ Clean")
    with col3:
        if st.button("🛑 End", type="secondary"):
            db.complete_session(session_id)
            st.session_state.voice_interview_active = False
            st.switch_page("pages/5_History.py")

    st.markdown("---")
    
    # Embed the Vapi widget
    if not VAPI_PUBLIC_KEY:
        st.error("`VAPI_PUBLIC_KEY` is missing in .env")
    else:
        components.iframe(f"http://localhost:{VAPI_PORT}", height=550, scrolling=False)

import streamlit as st
import os
import re
import tempfile
import traceback
from datetime import datetime
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# ==========================================
# 初期設定・定数・認証（Streamlit Secrets対応）
# ==========================================
st.set_page_config(page_title="Custom Gemini Pro App", layout="wide")

api_key = st.secrets.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
if not api_key:
    st.error("⚠️ GEMINI_API_KEY が設定されていません。StreamlitのSecretsに設定してください。")
    st.stop()

client = genai.Client(api_key=api_key)
DRIVE_FOLDER_ID = st.secrets.get("DRIVE_FOLDER_ID", "")

# ※APIで実際に有効なモデルIDを指定してください（例: gemini-2.5-pro, gemini-2.5-flash 等）
MODELS = {
    "gemini-2.5-pro": {"supports_cache": True, "max_out": 8192},
    "gemini-2.5-flash": {"supports_cache": True, "max_out": 8192},
    "gemini-3.5-flash": {"supports_cache": True, "max_out": 8192},
}

# ==========================================
# セッション状態の初期化
# ==========================================
if "threads" not in st.session_state:
    st.session_state.threads = {"Thread 1": []}
if "current_thread" not in st.session_state:
    st.session_state.current_thread = "Thread 1"
if "confirm_step" not in st.session_state:
    st.session_state.confirm_step = False
if "pending_payload" not in st.session_state:
    st.session_state.pending_payload = None
if "active_cache_name" not in st.session_state:
    st.session_state.active_cache_name = None

# ==========================================
# ヘルパー関数群
# ==========================================
def save_code_to_drive(text: str, thread_name: str):
    """テキストからコードブロックを抽出しGoogle Driveに保存する"""
    if not text:
        return
    if "gcp_service_account" not in st.secrets or not DRIVE_FOLDER_ID:
        st.toast("⚠️ Google Drive連携の設定がSecretsにないため保存をスキップしました。")
        return

    pattern = r"```(\w+)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)

    if not matches:
        return

    try:
        creds_info = dict(st.secrets["gcp_service_account"])
        creds = service_account.Credentials.from_service_account_info(
            creds_info, scopes=['https://www.googleapis.com/auth/drive.file']
        )
        service = build('drive', 'v3', credentials=creds)

        for idx, (lang, code) in enumerate(matches):
            ext = lang if lang else "txt"
            ext_map = {"python": "py", "javascript": "js", "latex": "tex", "tex": "tex", "html": "html"}
            ext = ext_map.get(ext.lower(), ext)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{thread_name}_{timestamp}_{idx+1}.{ext}"

            file_metadata = {'name': filename, 'parents': [DRIVE_FOLDER_ID]}
            media = MediaIoBaseUpload(io.BytesIO(code.encode('utf-8')), mimetype='text/plain')

            service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            st.toast(f"✅ コードをDriveに保存しました: {filename}")
    except Exception as e:
        st.error(f"Drive保存エラー: {e}")

def process_uploaded_files(uploaded_files):
    """StreamlitのUploadedFileをGemini API用のパーツに変換する"""
    api_parts = []
    display_names = []

    for uf in uploaded_files:
        display_names.append(uf.name)
        if uf.name.endswith(('.tex', '.txt', '.py', '.md', '.csv')):
            text_content = uf.getvalue().decode('utf-8')
            api_parts.append(f"\n--- File: {uf.name} ---\n{text_content}\n---\n")
        else:
            suffix = os.path.splitext(uf.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uf.getvalue())
                tmp_path = tmp.name

            uploaded_gemini_file = client.files.upload(file=tmp_path)
            api_parts.append(uploaded_gemini_file)
            os.remove(tmp_path)

    return api_parts, display_names

# ==========================================
# サイドバー: 設定・スレッド・キャッシュ管理
# ==========================================
with st.sidebar:
    st.header("🗂️ スレッド管理")
    new_thread_name = st.text_input("新規スレッド名")
    if st.button("スレッド作成") and new_thread_name:
        if new_thread_name not in st.session_state.threads:
            st.session_state.threads[new_thread_name] = []
            st.session_state.current_thread = new_thread_name
            st.rerun()

    selected_thread = st.selectbox(
        "現在のスレッド",
        list(st.session_state.threads.keys()),
        index=list(st.session_state.threads.keys()).index(st.session_state.current_thread)
    )
    st.session_state.current_thread = selected_thread

    st.divider()
    st.header("⚙️ モデル設定")
    selected_model = st.selectbox("モデルを選択", list(MODELS.keys()))
    max_output_tokens = st.slider("最大出力トークン数", 256, MODELS[selected_model]["max_out"], 2048)

    supports_cache = MODELS[selected_model]["supports_cache"]
    st.divider()
    st.header("⚡ コンテキストキャッシュ")

    if not supports_cache:
        st.info(f"`{selected_model}` は手動キャッシュ非対応のため、自動的にオフになっています。")
        use_cache = False
        st.session_state.active_cache_name = None
    else:
        use_cache = st.toggle("キャッシュ機能を利用する", value=False)
        if use_cache:
            st.caption("※最低約32,768トークン以上のデータが必要です。")
            cache_prompt = st.text_area("固定システムプロンプト/前提知識")
            cache_files = st.file_uploader("固定ファイル (PDF/画像/TeX等)", accept_multiple_files=True, key="cache_uploader")
            ttl_minutes = st.number_input("キャッシュ保持時間(分)", min_value=5, value=60)

            if st.button("キャッシュを作成・更新"):
                with st.spinner("キャッシュを作成中..."):
                    try:
                        c_parts, _ = process_uploaded_files(cache_files) if cache_files else ([], [])
                        if cache_prompt:
                            c_parts.insert(0, cache_prompt)

                        cache = client.caches.create(
                            model=selected_model,
                            config=types.CreateCachedContentConfig(
                                contents=c_parts,
                                ttl=f"{ttl_minutes * 60}s"
                            )
                        )
                        st.session_state.active_cache_name = cache.name
                        st.success(f"キャッシュ作成完了!\nID: `{cache.name}`")
                    except Exception:
                        st.error("キャッシュ作成失敗:")
                        st.code(traceback.format_exc())

            if st.session_state.active_cache_name:
                st.success("有効なキャッシュ: あり")

# ==========================================
# メイン画面: チャット履歴表示（ターミナル風にそのまま出力）
# ==========================================
st.title(f"💬 {st.session_state.current_thread}")

current_history = st.session_state.threads[st.session_state.current_thread]

for msg in current_history:
    with st.chat_message(msg["role"]):
        # Markdown解釈をせず、生テキスト（ターミナル出力）としてそのまま表示
        st.code(msg["display"], language=None)

# ==========================================
# メイン画面: 入力フォームと2段階確認
# ==========================================
st.divider()

if not st.session_state.confirm_step:
    uploaded_files = st.file_uploader(
        "ファイルを添付 (PDF, JPEG, PNG, TeX, TXT等)",
        type=["pdf", "jpeg", "jpg", "png", "tex", "txt", "py"],
        accept_multiple_files=True
    )
    user_prompt = st.text_area("プロンプトを入力してください", height=100)

    if st.button("送信確認へ進む (トークン計算)", type="primary"):
        if not user_prompt and not uploaded_files:
            st.warning("プロンプトまたはファイルを入力してください。")
        else:
            with st.spinner("ファイルを処理し、トークンを計算中..."):
                new_parts, file_names = process_uploaded_files(uploaded_files) if uploaded_files else ([], [])
                if user_prompt:
                    new_parts.append(user_prompt)

                display_text = user_prompt
                if file_names:
                    display_text = f"[添付: {', '.join(file_names)}]\n\n" + display_text

                api_contents = []
                for h in current_history:
                    api_contents.append(types.Content(role=h["role"], parts=h["api_parts"]))

                current_user_content = types.Content(role="user", parts=[
                    types.Part.from_uri(file_uri=p.uri, mime_type=p.mime_type) if hasattr(p, 'uri') else types.Part.from_text(text=p)
                    for p in new_parts
                ])
                api_contents.append(current_user_content)

                try:
                    token_resp = client.models.count_tokens(
                        model=selected_model,
                        contents=api_contents
                    )
                    input_tokens = token_resp.total_tokens
                except Exception:
                    st.error("トークン計算エラー:")
                    st.code(traceback.format_exc())
                    st.stop()

                st.session_state.pending_payload = {
                    "api_contents": api_contents,
                    "raw_parts_for_history": current_user_content.parts,
                    "display_text": display_text,
                    "input_tokens": input_tokens
                }
                st.session_state.confirm_step = True
                st.rerun()

else:
    payload = st.session_state.pending_payload
    st.warning("⚠️ **まだ送信されていません。以下の内容で送信しますか？**")

    col1, col2, col3 = st.columns(3)
    col1.metric("推定 入力トークン数 (履歴込)", f"{payload['input_tokens']:,} tokens")
    col2.metric("最大 出力トークン数 (上限設定)", f"{max_output_tokens:,} tokens")
    col3.metric("キャッシュ利用", "ON" if (use_cache and st.session_state.active_cache_name) else "OFF")

    with st.expander("送信内容のプレビュー", expanded=True):
        st.code(payload["display_text"], language=None)

    btn_col1, btn_col2 = st.columns([1, 5])

    if btn_col1.button("🚀 本当に送信する", type="primary"):
        with st.spinner("Geminiが応答を生成中..."):
            try:
                gen_config = types.GenerateContentConfig(
                    max_output_tokens=max_output_tokens,
                )
                if use_cache and st.session_state.active_cache_name:
                    gen_config.cached_content = st.session_state.active_cache_name

                response = client.models.generate_content(
                    model=selected_model,
                    contents=payload["api_contents"],
                    config=gen_config
                )

                # response.text が None の場合でも落ちないようにガード
                ai_text = response.text if response.text is not None else f"[空の応答が返却されました]\n{response}"

                st.session_state.threads[st.session_state.current_thread].append({
                    "role": "user",
                    "api_parts": payload["raw_parts_for_history"],
                    "display": payload["display_text"]
                })
                st.session_state.threads[st.session_state.current_thread].append({
                    "role": "model",
                    "api_parts": [types.Part.from_text(text=ai_text)],
                    "display": ai_text
                })

                save_code_to_drive(ai_text, st.session_state.current_thread)

                # 成功時のみ確認状態をリセットして画面を更新する
                st.session_state.confirm_step = False
                st.session_state.pending_payload = None
                st.rerun()

            except Exception:
                # エラー時はリロードせず、ターミナルと同じトレースバックをそのまま表示
                st.error("API送信エラーが発生しました（履歴は保存されていません）:")
                st.code(traceback.format_exc())

    if btn_col2.button("キャンセルして編集に戻る"):
        st.session_state.confirm_step = False
        st.session_state.pending_payload = None
        st.rerun()

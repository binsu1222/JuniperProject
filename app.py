from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os, uuid, requests, json, re, time
from typing import Optional, Dict
from datetime import datetime, timedelta, timezone
import logging


KST = timezone(timedelta(hours=9))

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# 게이트웨이(nginx:443)만 사용하도록 명칭·설정 통일
# -----------------------------------------------------------------------------
GATEWAY_BASE = os.environ.get("GATEWAY_BASE", "https://223.194.92.152:443") # 예: https://<DGX_HOST>
GW_VERIFY    = os.environ.get("GW_VERIFY", "false").lower() == "true" # 자체서명 TLS면 false
MODEL_NAME   = os.environ.get("MODEL_NAME", "gpt-oss:20b")

# 타임아웃 권장(문서 요구)
LLM_TIMEOUT_S    = int(os.environ.get("LLM_TIMEOUT_S", "30"))  # Ollama 최대 300s
DETECT_TIMEOUT_S = int(os.environ.get("DETECT_TIMEOUT_S", "60"))
AGENT_TIMEOUT_S  = int(os.environ.get("AGENT_TIMEOUT_S", "60"))

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app, resources={r"/*": {"origins": "*"}})

ALARM_STORE = []
MAX_ALARMS = 3
LATEST_RESULT_STORE = {"result": {"text": ""}, "timestamp": None, "scenario_id" : None}

# -----------------------------------------------------------------------------
# 템플릿 뷰 (업로드한 기본 디자인 사용)
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("mainpage.html")

@app.route("/detail")
def detail():
    return render_template("detailpage.html")

# -----------------------------------------------------------------------------
# 게이트웨이 래퍼 (Ollama / Detect / Agent) — nginx:443 경유만
# -----------------------------------------------------------------------------
def gw_get(path: str, timeout: int):
    """게이트웨이를 통한 GET 요청"""
    url = f"{GATEWAY_BASE}{path}"
    try:
        r = requests.get(url, timeout=timeout, verify=GW_VERIFY)
        r.raise_for_status()
        return r
    except requests.exceptions.RequestException as e:
        logger.error(f"Gateway GET error for {path}: {e}")
        raise

def gw_post(path: str, body: dict, timeout: int, stream: bool = False):
    """게이트웨이를 통한 POST 요청"""
    url = f"{GATEWAY_BASE}{path}"
    try:
        r = requests.post(url, json=body, timeout=timeout, verify=GW_VERIFY, stream=stream)
        r.raise_for_status()
        return r
    except requests.exceptions.RequestException as e:
        logger.error(f"Gateway POST error for {path}: {e}")
        raise

# Ollama - tags
def llm_tags():
    r = gw_get("/ollama/api/tags", timeout=LLM_TIMEOUT_S)
    return r.json() if "application/json" in r.headers.get("Content-Type","") else r.text

# Ollama - generate (blocking/stream)
def llm_generate(payload: dict, stream: bool = False):
    r = gw_post("/api/generate", payload, timeout=LLM_TIMEOUT_S, stream=stream)
    return r


# Agent
def agent_process(body: dict) -> dict:
    r = gw_post("/process-anomaly", body, timeout=AGENT_TIMEOUT_S)
    
    
    try:
        r = gw_post("/process-anomaly", body, timeout=AGENT_TIMEOUT_S)
        return r.json() if "application/json" in r.headers.get("Content-Type", "") else {"raw": r.text}
    except Exception as e:
        logger.error(f"Agent process failed: {e}")
        raise # 예외를 다시 던져 상위 호출자가 처리하도록 함

# def agent_health() -> dict:
#     r = gw_get("/agent/health", timeout=10)
#     return r.json() if "application/json" in r.headers.get("Content-Type","") else {"raw": r.text}

# -----------------------------------------------------------------------------
# 헬스 — 게이트웨이 사양과 동일
# -----------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    return Response("ok\n", mimetype="text/plain")

@app.route("/health")
def health():
    status = "connected"
    services = {}
    
    # 각 서비스 상태 체크
    try:
        #_ = llm_tags()
        services["ollama"] = "ok"
    except Exception as e:
        services["ollama"] = f"error: {str(e)}"
        status = "degraded"
    
    # try:
    #     _ = agent_health()
    #     services["agent"] = "ok"
    # except Exception as e:
    #     services["agent"] = f"error: {str(e)}"
    #     status = "degraded"
    
    # Detect 서비스는 간단한 헬스체크가 없으므로 스킵하거나 더미 요청으로 테스트
    services["detector"] = "unknown"
    
    return jsonify({
        "status": status, 
        "gateway": GATEWAY_BASE, 
        "model": MODEL_NAME,
        "services": services,
        "timestamp": time.time()
    })

# -----------------------------------------------------------------------------
# 기존 분석 API (프런트 연동 유지) - 규칙기반 + LLM 통합
# -----------------------------------------------------------------------------


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    
    payload = request.get_json(silent=True) or {}
    query = payload.get("query", "")
    

    if not query.strip(): 
        return jsonify({"error": "Query is required"}), 400

    logger.info(f"Analyzing query: {query[:100]}...")
    
    try:
        llm_body = {"model": MODEL_NAME, "prompt": query, "stream": False}
        r = llm_generate(llm_body, stream=False)
        r.raise_for_status()
        
        data = r.json()
        llm_text = data.get("response", "")
        
        return jsonify({"status": "Analysis completed",
                    "llm_response": llm_text,
                    "query": query
        }), 202
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ollama API request failed: {e}")
        return jsonify({"error": f"Ollama API request failed: {str(e)}"}), 502
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return jsonify({"error": f"Analysis failed: {str(e)}"}), 500
        
        
        '''# analysis, alarms는 비워서 반환
        return jsonify({
            "analysis_id": str(uuid.uuid4()),
            "alarms": [],
            "analysis": {"summary": "", "details": []},
            "query": query,
            "timestamp": datetime.now(KST)
        })
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return jsonify({"error": f"Analysis failed: {str(e)}"}), 500 '''  

    '''try:
        # RuleDetector 삭제: alarms와 analysis를 빈 상태로 초기화
        alarms, analysis = [], {"summary": "", "details": []}
        
        # LLM 추가 분석 (선택적)
        llm_body = {"model": MODEL_NAME, "prompt": query, "stream": False}
        llm_ok, llm_text, llm_err = True, "", None
        
        try:
            r = llm_generate(llm_body, stream=False)
            data = r.json()
            llm_text = data.get("response", "")
            
            # LLM 결과를 분석에 통합
            #if llm_text and len(llm_text.strip()) > 10:
            #   analysis["llm_insight"] = llm_text[:500] + ("..." if len(llm_text) > 500 else "")
            
            return jsonify({
            "analysis_id": str(uuid.uuid4()),
            "alarms": alarms,
            "analysis": analysis,
            "llm": {"model": MODEL_NAME, "ok": llm_ok, "text": llm_text if llm_ok else "", "error": llm_err},
            "query": query,
            "timestamp": time.time()
            })
                
        except Exception as e:
            llm_ok, llm_err = False, str(e)
            logger.warning(f"LLM analysis failed: {e}")

        
        
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return jsonify({"error": f"Analysis failed: {str(e)}"}), 500'''        
    

# -----------------------------------------------------------------------------
# 자연어 → JSON 추출 → Detect 자동 POST (게이트웨이 경유)
# -----------------------------------------------------------------------------
@app.route("/api/alerts/<scenario_id>", methods=["POST"])
def api_alerts(scenario_id):
    
    body = request.get_json(silent=True) or {}
    logging.info(f"[웹서버 수신] 시나리오 {scenario_id}, 데이터: {body}")
    
    
    DEVICE_MAP = {4: "CR1", 5: "BB1", 6: "BB2"}
    #DEVICE_MAP = {4: "CR1(ACX7509)", 5: "BB1(MX960)", 6: "BB2(MX480)"}
    
    if not body:
        return jsonify({"error": "Request body is required"}), 400

    unified_alarm_data = {}
    if scenario_id == "1":
        # 시나리오 1: JSON에서 alarm_type을 직접 가져옴
        device_id = body.get("device")
        device_name = DEVICE_MAP.get(device_id, f"Unknown({device_id})")
        interface_name = body.get("interface")
        packet_loss = body.get("packet_loss")
        time = body.get("timestamp")
        alarm_type = body.get("alarm_type") # 요청 JSON에서 alarm_type을 직접 가져옵니다.
        interlink_status = body.get("interlink_status")
        
        
        optic_power_data = body.get("optic_power", {})
        
        if not all([device_id, interface_name, packet_loss is not None, alarm_type]):
            return jsonify({"error": "Missing data for scenario 1"}), 400
        
        if (interlink_status == "false"):
            art = f"*경고 : {interface_name}가 down되어있습니다. interlink를 확인해주세요."
        else:
            art = ""
        
        unified_alarm_data = {
            "id": str(uuid.uuid4()),
            "device" : device_name,
            "title": f"{device_name} 장비 Interface {interface_name} CRC Error 발생",
            "description": f"Interface {interface_name} error rate - {packet_loss}%<br>{art}",
            "type": alarm_type, # JSON에서 받은 alarm_type을 그대로 사용합니다.
            "time": time,
            "optic_power_data": optic_power_data,
            "show_optic_power_button": True,
            "interface_name" : interface_name
        }
        
    elif scenario_id == "3":
        # 시나리오 3: 복잡한 트래픽 및 드롭 카운트 알람
        device_id = body.get("device")
        device_name = DEVICE_MAP.get(device_id, f"Unknown({device_id})")
        interface_name = body.get("interface")
        interfaces_list = body.get("interfaces", [])
        inout_packet = body.get("inout_packet", {})
        time = body.get("timestamp")
        drop_count = body.get("drop_count", {})
        
        if not all([device_id, interface_name, interfaces_list, inout_packet, drop_count]):
            return jsonify({"error": "Missing data for scenario 3"}), 400
        
        
        severity = inout_packet.get("alarm_type")
        rate = inout_packet.get('traffic_diff', 0.0)
        
        description_text = (
            f"Service Impact Rate - {rate}%"
        )
        
                    
        unified_alarm_data = {
            "id": str(uuid.uuid4()),
            "title": f"{device_name} Interface {interface_name}에서 Blackhole 발생",
            "description": description_text,
            "type": severity,
            "time": time,
            "show_optic_power_button": False,
            "show_interfaces_button": True, 
            "interfaces": interfaces_list
        }

    elif scenario_id == "2":
        # 시나리오 2: FPC 오류 및 트래픽/패킷 드롭 알람
        device_id = body.get("device")
        device_name = DEVICE_MAP.get(device_id, f"Unknown({device_id})")
        fpc_slot = body.get("fpc_slot") 
        log_description = "fpc/8/pfe/0/cm/0/XMCHIP(0)/0/XMCHIP_CMERROR_FI_PAR_PROTECT_FSET_REG_DETECTED_CP_FREEPOOL (0x70134) in module: XMCHIP(0) with scope: pfe category: functional level: major, oc_category: default"
        time = body.get("timestamp")
        discard_rate = body.get("discard_rate", {}).get("discard_rate", 0.0)
        #traffic_volume_rate_list = body.get("traffic_volume_rate", {}).get("traffic_volume_rate", [])
        
        # 필수 데이터 누락 확인
        if not all([device_id, fpc_slot, log_description, discard_rate]):
            return jsonify({"error": "Missing data for scenario 2"}), 400

        # traffic_volume_rate와 discard_rate 리스트에서 가장 높은 심각도 찾기

        severity = 'major'
 

        unified_alarm_data = {
            "id": str(uuid.uuid4()),
            "title": f"{device_name} 장비 FPC{fpc_slot} PFE Disable 발생",
            "description": f"Service Impact Rate - {discard_rate}%",
            "type": severity,
            "time": time,
            "show_optic_power_button": False,
            "show_interfaces_button": False,
            "show_log_button": True,
            #"traffic_data": traffic_volume_rate_list, 
            "discard_data": discard_rate, 
            "log_description": log_description,
        }
    
    else:
        # 시나리오 (미정) 또는 기타 시나리오
        return jsonify({"error": "invalid or unsupported scenario_id"}), 400
    
    global ALARM_STORE
    #if ALARM_STORE and unified_alarm_data["title"] == ALARM_STORE[-1]["title"] and unified_alarm_data["description"] == ALARM_STORE[-1]["description"]:
    #    return jsonify({"status": "no new alarms", "alarms": ALARM_STORE}), 200
    
    if len(ALARM_STORE) >= MAX_ALARMS:
        ALARM_STORE.pop(0)
    
    ALARM_STORE.append(unified_alarm_data)
        
    return jsonify({"status": "success", "alarms": ALARM_STORE}), 200

@app.route("/api/anomaly/process/<actionId>", methods=["POST"])
def process_anomaly_action_proxy(actionId):
    """
    클라이언트의 조치 요청을 받아 외부 조치 서버로 전달합니다.
    """
    # 프론트엔드로부터 받은 JSON 페이로드를 그대로 사용합니다.
    payload = request.get_json(silent=True) or {}
    
    external_url = f"{GATEWAY_BASE}/api/anomaly/process/{actionId}"
    
    logger.info(f"외부 조치 서버로 요청 프록시: {external_url}")

    try:
        response = requests.post(
            external_url, 
            json=payload, 
            timeout=AGENT_TIMEOUT_S, 
            verify=GW_VERIFY
        )
        response.raise_for_status()
        
        logger.info(f"ID '{actionId}'에 대한 조치 요청 성공적으로 프록시됨")
        return jsonify(response.json()), response.status_code
        
    except requests.exceptions.RequestException as e:
        logger.error(f"외부 조치 서버 통신 실패: {e}")
        return jsonify({
            "status": "error",
            "message": f"외부 조치 서버 통신 실패: {str(e)}"
        }), 502


@app.route("/api/current-alarms", methods=["GET"])
def get_current_alarms():
    if ALARM_STORE:
        return jsonify(ALARM_STORE), 200
    return jsonify({"status": "no new alarm"}), 202


@app.route("/api/alarms/ignore/<alarm_id>", methods=["DELETE"])
def api_ignore_alarm(alarm_id):
    global ALARM_STORE
    try:
        # ID를 기반으로 알람을 찾아서 삭제
        original_len = len(ALARM_STORE)
        ALARM_STORE = [alarm for alarm in ALARM_STORE if alarm.get("id") != alarm_id]
        if len(ALARM_STORE) < original_len:
            logger.info(f"Alarm with ID {alarm_id} ignored and removed.")
            return jsonify({"status": "success", "message": "Alarm ignored and removed"}), 200
        else:
            return jsonify({"error": "Alarm ID not found"}), 404
    except Exception as e:
        logger.error(f"Failed to ignore alarm: {e}")
        return jsonify({"error": "Failed to ignore alarm"}), 500
    


# -----------------------------------------------------------------------------
# LLM Agent 연계(선택 경로) — 게이트웨이 경유
# -----------------------------------------------------------------------------
@app.route("/api/agent/process", methods=["POST"])
def api_agent_process():
    logger.info("POST /api/agent/process 호출됨")
    body = request.get_json(silent=True) or {}
    
    if not body:
        return jsonify({"error": "Request body is required"}), 400
    
    
   # new_result = {
     #   "result": {"text": body.get("text")},
     #   "timestamp": time.time(),
    #    "scenario_id": body.get("scenario_id")
    #}
    # 받은 데이터를 전역 딕셔너리에 저장
    
    global LATEST_RESULT_STORE
    LATEST_RESULT_STORE["result"] = {"text": body.get("text")}
    LATEST_RESULT_STORE["timestamp"] = time.time()
    LATEST_RESULT_STORE["scenario_id"] = body.get("scenario_id")
    
    logger.info(f"Agent analysis received with scenario ID: {LATEST_RESULT_STORE['scenario_id']}")
    
    
    # AI Agent에게 성공적으로 받았다는 응답을 보냄
    return jsonify({"status": "success", "message": "Result stored"}), 200

# 프론트엔드가 결과를 가져갈 수 있는 GET 엔드포인트
@app.route("/api/results/latest-result", methods=["GET"])
def get_analysis_results():
    if LATEST_RESULT_STORE["result"]:
        # 결과가 있으면 초기화 후 반환
        result = LATEST_RESULT_STORE.copy()
        LATEST_RESULT_STORE["result"] = None
        LATEST_RESULT_STORE["timestamp"] = None
        LATEST_RESULT_STORE["scenario_id"] = None
        logger.info("Analysis result found and sent. Data cleared.")
        return jsonify(result), 200
    else:
        # 결과가 아직 없으면 "처리 중" 상태를 반환
        logger.info("No new analysis result available.")
        return jsonify({"status": "processing"}), 202

# @app.route("/api/agent/health", methods=["GET"])
# def api_agent_health():
#     try:
#         result = agent_health()
#         return jsonify(result)
#     except Exception as e:
#         logger.error(f"Agent health check failed: {e}")
#         return jsonify({"error": str(e)}), 502

# -----------------------------------------------------------------------------
# SDN 샘플(프런트 버튼용) - 실제 SDN 컨트롤러 연동 시 수정 필요
# -----------------------------------------------------------------------------
@app.route("/api/sdn/send", methods=["POST"])
def api_sdn_send():
    payload = request.get_json(silent=True) or {}
    
    # 실제 SDN 컨트롤러가 있다면 여기서 호출
    # 예: sdn_controller.send_policy_update(payload)
    
    logger.info(f"SDN controller request: {payload}")
    
    # 시뮬레이션 응답
    return jsonify({
        "ok": True, 
        "message": "SDN 컨트롤러에 전송 완료", 
        "echo": payload,
        "timestamp": time.time(),
        "controller_response": "Policy updated successfully"
    })

# -----------------------------------------------------------------------------
# 에러 핸들러
# -----------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({"error": "Internal server error"}), 500

# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    
    logger.info(f"Starting Flask app on port {port}, debug={debug}")
    logger.info(f"Gateway: {GATEWAY_BASE}, Model: {MODEL_NAME}")
    
    app.run(host="0.0.0.0", port=port, debug=debug)

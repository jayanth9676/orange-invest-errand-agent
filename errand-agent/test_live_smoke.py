import urllib.request
import re
import json
import time
import sys

def main():
    base = 'http://localhost:8000'
    print("=" * 60)
    print("  LIVE FASTAPI ENDPOINT & BROWSER SMOKE TEST (http://localhost:8000)")
    print("=" * 60)

    # 1. GET / & GET /index.html
    with urllib.request.urlopen(base + '/') as r:
        assert r.status == 200
        html_root = r.read().decode('utf-8')
        assert '<html' in html_root.lower() or '<!doctype' in html_root.lower()
        print(f"[PASS] GET / -> 200 OK (HTML bytes: {len(html_root)})")

    with urllib.request.urlopen(base + '/index.html') as r:
        assert r.status == 200
        html_idx = r.read().decode('utf-8')
        assert html_root == html_idx
        print(f"[PASS] GET /index.html -> 200 OK (Identical to GET /)")

    # 2. Assets verification
    assets = sorted(list(set(re.findall(r'/assets/[a-zA-Z0-9_\-\.]+\.(?:js|css)', html_root))))
    print(f"Discovered assets in HTML: {assets}")
    assert len(assets) > 0, "No assets found in index.html"
    for a in assets:
        with urllib.request.urlopen(base + a) as r:
            assert r.status == 200
            ct = r.headers.get('Content-Type')
            content = r.read()
            assert len(content) > 0
            print(f"[PASS] Asset {a} -> 200 OK ({ct}, {len(content)} bytes)")

    # 3. Real-time SSE endpoints (/events and /api/trace-stream)
    for sse_path in ['/events', '/api/trace-stream']:
        req = urllib.request.Request(base + sse_path, headers={'Accept': 'text/event-stream'})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            ct = r.headers.get('Content-Type', '')
            cc = r.headers.get('Cache-Control', '')
            assert 'text/event-stream' in ct, f"Expected text/event-stream, got {ct}"
            assert 'no-cache' in cc, f"Expected no-cache, got {cc}"
            print(f"[PASS] SSE {sse_path} -> 200 OK (Content-Type: {ct}, Cache-Control: {cc})")

    # 4. /api/state
    with urllib.request.urlopen(base + '/api/state') as r:
        assert r.status == 200
        state = json.loads(r.read().decode('utf-8'))
        fsm_st = state.get('fsm_state')
        tot_mem = state.get('total_memories_seeded')
        print(f"[PASS] /api/state -> 200 OK (FSM State: {fsm_st}, Seeded Memories: {tot_mem})")

    # 5. Interactive chat journey
    # 5.1 Reset
    req_reset = urllib.request.Request(base + '/api/reset', data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req_reset) as r:
        assert r.status == 200
        print("[PASS] POST /api/reset -> 200 OK (Demo state reset)")

    # 5.2 Turn 1: Discovery Request
    payload1 = json.dumps({"message": "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"}).encode('utf-8')
    req1 = urllib.request.Request(base + '/api/chat', data=payload1, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req1) as r:
        assert r.status == 200
        data1 = json.loads(r.read().decode('utf-8'))
        st1 = data1.get('fsm_state')
        prop1 = data1.get('active_proposal')
        print(f"[PASS] POST /api/chat (Discovery) -> 200 OK (FSM State: {st1})")
        if prop1:
            print(f"       Sentinel Proposal: Provider={prop1.get('provider_id')}, Slot={prop1.get('slot')}, Price=₹{prop1.get('final_price')}")
            print(f"       SHA-256 Digest: {prop1.get('payload_hash')}")

    # 5.3 Turn 2: Affirmation Request
    payload2 = json.dumps({"message": "Yes, confirm"}).encode('utf-8')
    req2 = urllib.request.Request(base + '/api/chat', data=payload2, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req2) as r:
        assert r.status == 200
        data2 = json.loads(r.read().decode('utf-8'))
        st2 = data2.get('fsm_state')
        reply2 = data2.get('reply', '')
        print(f"[PASS] POST /api/chat (Affirmation) -> 200 OK (FSM State: {st2})")
        print(f"       Assistant Reply: {reply2[:100]}...")

    print("\n[ALL LIVE ENDPOINT & BROWSER SMOKE CHECKS PASSED]")
    return 0

if __name__ == "__main__":
    sys.exit(main())

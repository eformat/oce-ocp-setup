#!/usr/bin/env python3
"""add-agent.py — roll out a new OCE agent in one idempotent run.

Stages (each idempotent; safe to re-run):
  1 namespace   OCC namespace + backing k8s namespace + platform tenant grants
  2 materials   OCC accounts (new users), secrets, credential sources
  3 agent       configuration (golden template) + agent + deploy + SCC + poll
  4 origin      append the agent's own Control-UI origin + redeploy + poll
  5 controlui   cookie secret + render/apply oauth-proxy bundle + SAR roles
                + namespace IAM bindings for every spec user
  6 summary

Usage:
  python3 add-agent.py --spec spec.json             # full rollout
  python3 add-agent.py --spec spec.json --check     # read-only environment validation
  python3 add-agent.py --spec spec.json --from 4    # run from stage 4 onward

All secret material moves through files (never argv, never echoed). Cluster
values (service IPs, endpoint pod IPs, image digests, cluster domain) are read
LIVE at run time — never from stale snapshots.
"""
import argparse
import base64
import json
import os
import secrets
import stat
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DEFAULT_INPUTS = os.path.join(HOME, ".local/state/openclaw-enterprise/rhoai")
DEFAULT_FORK = os.path.join(HOME, "git/openclaw-enterprise")
DEFAULT_KUBECONFIG = os.path.join(HOME, ".kube/config.ocp5-2")

NAMESPACE_OWNER_PERMISSIONS = [
    {"action": "read", "resourceKind": "namespace"},
    {"action": "read", "resourceKind": "agent"},
    {"action": "update", "resourceKind": "agent"},
    {"action": "delete", "resourceKind": "agent"},
    {"action": "deploy", "resourceKind": "agent"},
    {"action": "operate", "resourceKind": "agent"},
    {"action": "administer", "resourceKind": "agent"},
    {"action": "read_logs", "resourceKind": "agent"},
    {"action": "manage_policy", "resourceKind": "agent"},
    {"action": "read", "resourceKind": "agent_revision"},
    {"action": "read", "resourceKind": "sandbox_policy"},
    {"action": "update", "resourceKind": "sandbox_policy"},
    {"action": "read", "resourceKind": "configuration"},
    {"action": "update", "resourceKind": "configuration"},
    {"action": "delete", "resourceKind": "configuration"},
    {"action": "read", "resourceKind": "credential_source"},
    {"action": "operate", "resourceKind": "credential_source"},
    {"action": "update", "resourceKind": "credential_source"},
    {"action": "delete", "resourceKind": "credential_source"},
    {"action": "read", "resourceKind": "secret"},
    {"action": "operate", "resourceKind": "secret"},
    {"action": "read", "resourceKind": "preset"},
]

PLATFORM_GRANTS = [
    "openclaw-enterprise-worker:oce-openclaw-tenant-worker",
    "openclaw-enterprise-api-observer:oce-openclaw-gateway-observer",
    "openclaw-enterprise-worker-openshell:oce-openshell-workspace-writer",
    "openclaw-enterprise-api-secrets:oce-openclaw-tenant-api",
    "openclaw-enterprise-api-configuration:oce-openclaw-tenant-configuration",
]


class Run:
    def __init__(self, spec_path, check_only=False, from_stage=1):
        self.spec_path = os.path.abspath(spec_path)
        self.spec = json.load(open(self.spec_path))
        self.check_only = check_only
        self.from_stage = from_stage
        self.inputs = os.path.expanduser(self.spec.get("inputsDir", DEFAULT_INPUTS))
        self.fork = os.path.expanduser(self.spec.get("forkDir", DEFAULT_FORK))
        self.base = self.spec["occUrl"].rstrip("/")
        self.kubeconfig = os.path.expanduser(self.spec.get("kubeconfig", DEFAULT_KUBECONFIG))
        self.env = dict(os.environ, KUBECONFIG=self.kubeconfig)
        self.state_file = os.path.join(
            self.inputs, f"add-agent-state-{self.spec['agent']}.json")
        self.state = {}
        if os.path.exists(self.state_file):
            self.state = json.load(open(self.state_file))

    # ---------- primitives ----------

    def _curl_auth(self):
        """argv-safe x-api-key auth config from the private service key file."""
        svc = json.load(open(os.path.join(
            self.inputs, self.spec.get("serviceKeyFile", "initial-admin-service-key.json"))))
        key = svc["data"]["key"]
        cfg = os.path.join(self.inputs, "occ-curl-auth.cfg")
        with open(cfg, "w") as f:
            f.write(f'header = "x-api-key: {key}"\n')
        os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)
        return cfg

    def api(self, method, path, body=None):
        cfg = self._curl_auth()
        args = ["curl", "-s", "-K", cfg, "-X", method, f"{self.base}{path}",
                "-o", "/tmp/add-agent-out.json", "-w", "%{http_code}"]
        if body is not None:
            bp = "/tmp/add-agent-body.json"
            with open(bp, "w") as f:
                json.dump(body, f)
            os.chmod(bp, stat.S_IRUSR | stat.S_IWUSR)
            args[2:2] = ["-H", "content-type: application/json", "-d", f"@{bp}"]
        for attempt in range(4):
            r = subprocess.run(args, capture_output=True, text=True, timeout=180)
            try:
                d = json.load(open("/tmp/add-agent-out.json"))
                if isinstance(d, dict) and ("data" in d or "error" in d):
                    return r.stdout, d
            except Exception:
                pass
            time.sleep(2 * (attempt + 1))
        return r.stdout, {}

    def kubectl(self, *args, stdin=None):
        return subprocess.run(["kubectl", *args], input=stdin, capture_output=True,
                              text=True, timeout=120, env=self.env)

    def oc(self, *args, stdin=None):
        return subprocess.run(["oc", *args], input=stdin, capture_output=True,
                              text=True, timeout=120, env=self.env)

    def save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=1)
        os.chmod(self.state_file, stat.S_IRUSR | stat.S_IWUSR)

    # ---------- live cluster reads ----------

    def live_values(self):
        """Every value the Control-UI render needs, read from the live cluster."""
        v = {}
        v["cluster_domain"] = self.oc("get", "ingress.config.openshift.io", "cluster",
                                      "-o", "jsonpath={.spec.domain}").stdout.strip()
        v["kube_api_svc"] = self.kubectl(
            "-n", "default", "get", "svc", "kubernetes",
            "-o", "jsonpath={.spec.clusterIP}").stdout.strip()
        eps = json.loads(self.kubectl(
            "-n", "default", "get", "endpoints", "kubernetes", "-o", "json").stdout)
        v["kube_api_endpoints"] = sorted({a["ip"] for s in eps["subsets"]
                                          for a in s.get("addresses", [])})
        v["dns_svc"] = self.kubectl(
            "-n", "openshift-dns", "get", "svc", "dns-default",
            "-o", "jsonpath={.spec.clusterIP}").stdout.strip()
        pods = json.loads(self.kubectl(
            "-n", "openshift-dns", "get", "pods", "-o", "json").stdout)
        v["dns_pod_ips"] = sorted({p["status"]["podIP"] for p in pods["items"]
                                   if p["status"].get("podIP")})
        v["oauth_svc"] = self.kubectl(
            "-n", "openshift-authentication", "get", "svc", "oauth-openshift",
            "-o", "jsonpath={.spec.clusterIP}").stdout.strip()
        pods = json.loads(self.kubectl(
            "-n", "openshift-authentication", "get", "pods", "-o", "json").stdout)
        v["oauth_pod_ips"] = sorted({p["status"]["podIP"] for p in pods["items"]
                                     if p["status"].get("podIP")})
        v["router_ip"] = self.kubectl(
            "-n", "openshift-ingress", "get", "svc", "router-default",
            "-o", "jsonpath={.spec.clusterIP}").stdout.strip()
        v["images"] = self.live_control_ui_images()
        return v

    def live_control_ui_images(self):
        """Full image digests from any Running oce-agent-control-ui-oauth pod."""
        out = self.kubectl("get", "pods", "-A", "-l",
                           "app.kubernetes.io/name=oce-agent-control-ui-oauth",
                           "-o", "json").stdout
        for p in json.loads(out or "{}").get("items", []):
            if all(c.get("state", {}).get("running") for c in p["status"].get("containerStatuses", [])):
                images = {c["name"]: c["image"] for c in p["spec"]["containers"]}
                if "oauth-proxy" in images and "identity-proxy" in images:
                    return images
        raise RuntimeError("no Running oce-agent-control-ui-oauth pod found to read images from")

    # ---------- stage 1: namespace ----------

    def stage_namespace(self):
        code, d = self.api("GET", "/namespaces")
        existing = next((n for n in d.get("data", []) if n["name"] == self.spec["namespace"]), None)
        if existing:
            self.state["ns"] = existing["id"]
            print(f"[ns] exists: {self.state['ns']}")
        else:
            code, d = self.api("POST", "/namespaces", {"name": self.spec["namespace"]})
            assert code == "201", f"namespace create: {code} {json.dumps(d)[:200]}"
            self.state["ns"] = d["data"]["id"]
            print(f"[ns] created: {self.state['ns']}")
        ns = self.state["ns"]

        kns = ""
        for _ in range(60):
            # fetch all and filter client-side: the label VALUE is an OCC resource
            # ID (ns_...) and IDs must never appear in command argv (argv-guard)
            r = self.kubectl("get", "namespaces", "-o", "json")
            for item in json.loads(r.stdout or "{}").get("items", []):
                if item["metadata"].get("labels", {}).get("openclaw.dev/namespace") == ns:
                    kns = item["metadata"]["name"]
                    break
            if kns:
                break
            time.sleep(2)
        assert kns, "backing kubernetes namespace never appeared"
        self.state["kns"] = kns
        print(f"[k8s] tenant namespace: {kns}")

        for grant in PLATFORM_GRANTS:
            binding, clusterrole = grant.split(":")
            account = ("openclaw-enterprise-worker" if "worker" in binding
                       else "openclaw-enterprise-api")
            r = self.oc("-n", kns, "create", "rolebinding", binding,
                        f"--clusterrole={clusterrole}",
                        f"--serviceaccount=openclaw-system:{account}",
                        "--dry-run=client", "-o", "yaml")
            assert r.returncode == 0, r.stderr
            r2 = self.oc("-n", kns, "apply", "-f", "-", stdin=r.stdout)
            assert r2.returncode == 0, r2.stderr
        print("[grants] platform RoleBindings applied")
        self.save_state()

    # ---------- stage 2: accounts, secrets, credential sources ----------

    def stage_materials(self):
        ns = self.state["ns"]
        principals_path = os.path.join(self.inputs, "team-principals.json")
        principals = json.load(open(principals_path)) if os.path.exists(principals_path) else {}

        # OCC accounts for users without one
        for user in self.spec["users"]:
            if user["name"] in principals:
                continue
            email = user.get("email")
            assert email, f"user {user['name']} needs an email (no principal on file)"
            pw = secrets.token_urlsafe(18)
            admin_pw = open(os.path.join(
                self.inputs, self.spec.get("adminPasswordFile", "initial-admin-password"))
            ).read().strip()
            admin_email = self.spec.get("adminEmail", "admin@admin.com")
            sb = "/tmp/add-agent-signin.json"
            with open(sb, "w") as f:
                json.dump({"email": admin_email, "password": admin_pw}, f)
            os.chmod(sb, stat.S_IRUSR | stat.S_IWUSR)
            subprocess.run(["curl", "-s", "-c", "/tmp/add-agent.cookies", "-X", "POST",
                            f"{self.base}/api/auth/sign-in/email", "-H", "Content-Type: application/json",
                            "-H", f"Origin: {self.base}", "-d", f"@{sb}"],
                           capture_output=True, text=True, timeout=60)
            ab = "/tmp/add-agent-account.json"
            with open(ab, "w") as f:
                json.dump({"email": email, "password": pw, "name": user.get("name").title()}, f)
            os.chmod(ab, stat.S_IRUSR | stat.S_IWUSR)
            r = subprocess.run(["curl", "-s", "-b", "/tmp/add-agent.cookies", "-X", "POST",
                                f"{self.base}/api/auth/accounts", "-H", "Content-Type: application/json",
                                "-H", f"Origin: {self.base}", "-d", f"@{ab}"],
                               capture_output=True, text=True, timeout=60)
            resp = json.loads(r.stdout)
            prn = resp["data"]["principalId"]
            principals[user["name"]] = prn
            json.dump(principals, open(principals_path, "w"), indent=1)
            pw_file = os.path.join(self.inputs, "team-accounts-passwords.json")
            pws = json.load(open(pw_file)) if os.path.exists(pw_file) else {}
            pws[f"{user['name']} ({email})"] = pw
            json.dump(pws, open(pw_file, "w"), indent=1)
            os.chmod(pw_file, stat.S_IRUSR | stat.S_IWUSR)
            os.remove(sb)
            os.remove(ab)
            print(f"[account] {user['name']} created: {prn} (temp password recorded)")
        os.remove("/tmp/add-agent.cookies") if os.path.exists("/tmp/add-agent.cookies") else None

        # secrets
        code, d = self.api("GET", f"/namespaces/{ns}/secrets")
        have = {s["name"]: s["id"] for s in d.get("data", [])}
        secret_ids = self.state.setdefault("secrets", {})
        for s in self.spec["secrets"]:
            if s["name"] in have:
                secret_ids[s["name"]] = have[s["name"]]
                print(f"[secret] {s['name']} exists: {have[s['name']]}")
                continue
            value = open(os.path.expanduser(s["valueFile"])).read().strip()
            code, d = self.api("POST", f"/namespaces/{ns}/secrets",
                               {"name": s["name"], "value": value})
            assert code == "201", f"secret {s['name']}: {code} {json.dumps(d)[:200]}"
            secret_ids[s["name"]] = d["data"]["id"]
            print(f"[secret] {s['name']} created: {d['data']['id']}")

        # credential sources
        code, d = self.api("GET", f"/namespaces/{ns}/credential-sources")
        have_cs = {c["name"]: c["id"] for c in d.get("data", [])}
        cs_ids = self.state.setdefault("sources", {})
        for c in self.spec["credentialSources"]:
            if c["name"] in have_cs:
                cs_ids[c["name"]] = have_cs[c["name"]]
                print(f"[cs] {c['name']} exists: {have_cs[c['name']]}")
                continue
            body = {"name": c["name"], "type": c["type"],
                    "secrets": {c["secretField"]: {"kind": "secret", "namespaceId": ns,
                                                   "id": secret_ids[c["secret"]]}}}
            if c.get("config"):
                body["config"] = c["config"]
            code, d = self.api("POST", f"/namespaces/{ns}/credential-sources", body)
            assert code == "201", f"cs {c['name']}: {code} {json.dumps(d)[:200]}"
            cs_ids[c["name"]] = d["data"]["id"]
            print(f"[cs] {c['name']} created: {d['data']['id']}")
        self.save_state()

    # ---------- stage 3: configuration, agent, deploy ----------

    def stage_agent(self):
        ns = self.state["ns"]
        tmpl = json.load(open(os.path.expanduser(
            self.spec.get("configTemplate",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "agent-config-template.json")))))
        values = json.loads(json.dumps(tmpl["values"]).replace(
            "__AGENT_DISPLAY_NAME__", self.spec.get("displayName", self.spec["agent"])))

        code, d = self.api("GET", f"/namespaces/{ns}/configurations/{self.state.get('configuration', '')}"
                           ) if self.state.get("configuration") else ("", {})
        if code == "200":
            cfg_id = self.state["configuration"]
            print(f"[cfg] exists: {cfg_id}")
        else:
            code, d = self.api("POST", f"/namespaces/{ns}/configurations",
                               {"kind": "agent", "values": values})
            assert code == "201", f"configuration: {code} {json.dumps(d)[:300]}"
            cfg_id = d["data"]["id"]
            print(f"[cfg] created: {cfg_id}")
        self.state["configuration"] = cfg_id

        code, d = self.api("GET", f"/namespaces/{ns}/agents")
        have_agt = {a["name"]: a["id"] for a in d.get("data", [])}
        if self.spec["agent"] in have_agt:
            agt_id = have_agt[self.spec["agent"]]
            print(f"[agent] exists: {agt_id}")
        else:
            harness = next(c for c in self.spec["credentialSources"] if c.get("harnessAuth"))
            body = {"name": self.spec["agent"], "configurationId": cfg_id,
                    "executionMode": "dedicated",
                    "harnessAuth": {"method": "credential_source", "sourceId": self.state["sources"][harness["name"]]},
                    "credentialSources": [{"sourceId": sid} for sid in self.state["sources"].values()]}
            code, d = self.api("POST", f"/namespaces/{ns}/agents", body)
            assert code == "201", f"agent: {code} {json.dumps(d)[:300]}"
            agt_id = d["data"]["id"]
            print(f"[agent] created: {agt_id}")
        self.state["agent"] = agt_id
        self.deploy_and_wait()
        self.save_state()

    def deploy_and_wait(self):
        ns, agt = self.state["ns"], self.state["agent"]
        code, d = self.api("POST", f"/namespaces/{ns}/agents/{agt}/deploy", {})
        assert code == "202", f"deploy: {code} {json.dumps(d)[:200]}"
        rev = d["data"]["id"]
        self.state["revision"] = rev
        print(f"[deploy] revision {rev}")

        admitted = self.state.get("scc_admitted")
        for i in range(60):
            r = self.kubectl("-n", self.state["kns"], "get", "sa", "-o", "name")
            gw_sa = next((s.split("/")[1] for s in (r.stdout or "").splitlines()
                          if s.startswith("serviceaccount/gateway-")), None)
            if gw_sa and not admitted:
                r1 = self.oc("-n", self.state["kns"], "create", "role",
                             "oce-fixed-uid-1000-agent-gateway", "--verb=use",
                             "--resource=securitycontextconstraints.security.openshift.io",
                             "--resource-name=oce-fixed-uid-1000", "--dry-run=client", "-o", "yaml")
                self.oc("-n", self.state["kns"], "apply", "-f", "-", stdin=r1.stdout)
                r2 = self.oc("-n", self.state["kns"], "create", "rolebinding",
                             "oce-fixed-uid-1000-agent-gateway",
                             "--role=oce-fixed-uid-1000-agent-gateway",
                             f"--serviceaccount={self.state['kns']}:{gw_sa}",
                             "--dry-run=client", "-o", "yaml")
                self.oc("-n", self.state["kns"], "apply", "-f", "-", stdin=r2.stdout)
                chk = self.oc("auth", "can-i", "use", "scc/oce-fixed-uid-1000",
                              "-n", self.state["kns"],
                              f"--as=system:serviceaccount:{self.state['kns']}:{gw_sa}")
                print(f"[scc] gateway SA {gw_sa}: can-i={chk.stdout.strip()}")
                self.state["scc_admitted"] = True
                admitted = True
            r = subprocess.run(["curl", "-s", "-K", self._curl_auth(),
                                f"{self.base}/namespaces/{ns}/agents/{agt}/deployments/{rev}"],
                               capture_output=True, text=True, timeout=60)
            try:
                status = (json.loads(r.stdout).get("data") or {}).get("status")
            except Exception:
                status = None
            if i % 3 == 0:
                print(f"[{i * 15}s] deploy status: {status}", flush=True)
            if status in ("succeeded", "ready", "active"):
                print("[deploy] SUCCEEDED")
                self.save_state()
                return
            if status in ("failed", "error"):
                raise RuntimeError("deploy FAILED — check worker logs (RUNTIME_AUTHENTICATION_FAILED etc.)")
            time.sleep(15)
        raise RuntimeError("deploy timeout")

    # ---------- stage 4: origin fix ----------

    def stage_origin(self):
        ns, cfg_id = self.state["ns"], self.state["configuration"]
        host = self.control_ui_host()
        code, d = self.api("GET", f"/namespaces/{ns}/configurations/{cfg_id}")
        assert code == "200", f"configuration get: {code}"
        values = d["data"]["values"]
        origins = values.setdefault("gateway", {}).setdefault(
            "controlUi", {}).setdefault("allowedOrigins", [])
        if f"https://{host}" not in origins:
            origins.append(f"https://{host}")
            code, d = self.api("PATCH", f"/namespaces/{ns}/configurations/{cfg_id}",
                               {"values": values})
            assert code == "200", f"configuration patch: {code}"
            print(f"[origin] allowedOrigins -> {origins}")
            self.deploy_and_wait()
        else:
            print(f"[origin] {host} already allowed")
        self.save_state()

    def control_ui_host(self):
        suffix = self.gateway_service()[len("gateway-"):]
        domain = self.live_values()["cluster_domain"]
        return f"oc-ui-{suffix}.{domain}"

    def gateway_service(self):
        r = self.kubectl("-n", self.state["kns"], "get", "sa", "-o", "name")
        return next(s.split("/")[1] for s in (r.stdout or "").splitlines()
                    if s.startswith("serviceaccount/gateway-"))

    # ---------- stage 5: control UI + access ----------

    def stage_controlui(self):
        kns = self.state["kns"]
        gw = self.gateway_service()
        suffix = gw[len("gateway-"):]
        v = self.live_values()
        host = f"oc-ui-{suffix}.{v['cluster_domain']}"
        self.state["control_ui_host"] = host

        # cookie secret (unique per agent)
        session_secret = secrets.token_hex(16)
        cookie = {"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": "oce-agent-control-ui-oauth-cookie", "namespace": kns},
                  "type": "Opaque",
                  "data": {"session_secret": base64.b64encode(session_secret.encode()).decode()}}
        cp = os.path.join(self.inputs, f"control-ui-oauth-cookie-{self.spec['agent']}.yaml")
        with open(cp, "w") as f:
            json.dump(cookie, f)
        os.chmod(cp, stat.S_IRUSR | stat.S_IWUSR)
        r = self.oc("apply", "-f", cp)
        assert r.returncode == 0, r.stderr
        print("[cookie] secret applied")

        # render from the fork base manifest (has the codified login-chain fixes)
        tmpl_path = os.path.expanduser(self.spec.get(
            "controlUiTemplate",
            os.path.join(self.fork, "deploy/rhoai/manifests/openshift/base/control-ui/control-ui-oauth-proxy.yaml")))
        tmpl = open(tmpl_path).read()
        values = {
            "GATEWAY_NAMESPACE": kns,
            "GATEWAY_SERVICE": gw,
            "CONTROL_UI_HOST": host,
            "OAUTH_SAR": json.dumps({"namespace": kns, "resource": "services",
                                     "resourceName": gw, "verb": "get"},
                                    separators=(",", ":")),  # compact: spaces break YAML scalars
            "OAUTH_HOST": f"oauth-openshift.{v['cluster_domain']}",
            "OAUTH_SERVICE_IP": v["oauth_svc"],
            "ROUTER_IP": v["router_ip"],
            "API_SERVICE_CIDR": f"{v['kube_api_svc']}/32",   # kubernetes.default.svc, NOT dns!
            "DNS_CIDR_0": f"{v['dns_svc']}/32",
            "OAUTH_PROXY_IMAGE": v["images"]["oauth-proxy"],
            "CONTROL_UI_ENVOY_IMAGE": v["images"]["identity-proxy"],
        }
        for var, val in values.items():
            tmpl = tmpl.replace("${" + var + "}", val)
        assert "${" not in tmpl, "unsubstituted placeholders remain"
        lines = tmpl.split("\n")

        def append_peer(cidr, peers):
            idx = [i for i, l in enumerate(lines) if l.strip() == f"cidr: {cidr}"]
            assert idx, f"cidr {cidr} not found"
            block = []
            for ip in peers:
                block += ["        - ipBlock:", f"            cidr: {ip}/32"]
            lines[idx[0] + 1:idx[0] + 1] = block

        # admit DNAT'd endpoint IPs beside each service VIP (OVN post-DNAT evaluation)
        append_peer(f"{v['dns_svc']}/32", [ip for ip in v["dns_pod_ips"] if ip != v["dns_svc"]])
        append_peer(f"{v['kube_api_svc']}/32", v["kube_api_endpoints"])
        append_peer(f"{v['oauth_svc']}/32", [ip for ip in v["oauth_pod_ips"] if ip != v["oauth_svc"]])

        out = os.path.join(self.inputs, f"control-ui-oauth-proxy-{self.spec['agent']}.yaml")
        with open(out, "w") as f:
            f.write("\n".join(lines))
        os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
        r = self.oc("apply", "-f", out)
        assert r.returncode == 0, r.stderr
        print("[proxy] control-ui bundle applied")

        for _ in range(10):
            r = self.oc("-n", kns, "get", "route", "-o", "json")
            routes = json.loads(r.stdout or "{}").get("items", [])
            if routes and routes[0]["spec"].get("host") == host:
                print(f"[route] {routes[0]['metadata']['name']} -> {host}")
                break
            time.sleep(2)

        # per-user SAR roles (Control-UI login authorization)
        for user in self.spec["users"]:
            role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                    "metadata": {"name": f"control-ui-sar-{user['name']}", "namespace": kns},
                    "rules": [{"apiGroups": [""], "resources": ["services"],
                               "resourceNames": [gw], "verbs": ["get"]}]}
            rb = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                  "metadata": {"name": f"control-ui-sar-{user['name']}", "namespace": kns},
                  "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                              "name": f"control-ui-sar-{user['name']}"},
                  "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User",
                                "name": user.get("openshiftUser", user["name"])}]}
            for obj, name in ((role, f"/tmp/add-agent-sar-role-{user['name']}.json"),
                              (rb, f"/tmp/add-agent-sar-rb-{user['name']}.json")):
                with open(name, "w") as f:
                    json.dump(obj, f)
                r = self.oc("apply", "-f", name)
                assert r.returncode == 0, r.stderr
                os.remove(name)
            print(f"[sar] {user['name']} bound to {gw}")

        # access for every user: exact-resource bindings on the agent + configuration +
        # credential sources (resource grants), PLUS the namespace binding (console
        # visibility). The namespace binding alone does NOT grant resource reads.
        code, d = self.api("GET", f"/namespaces/{self.state['ns']}/iam/roles")
        roles = d.get("data") or []
        role_id = next((r0["id"] for r0 in roles if r0.get("name") == "namespace_owner"), None)
        if not role_id:
            code, d = self.api("POST", f"/namespaces/{self.state['ns']}/iam/roles",
                               {"name": "namespace_owner", "permissions": NAMESPACE_OWNER_PERMISSIONS})
            assert code == "201", f"role: {code} {json.dumps(d)[:200]}"
            role_id = d["data"]["id"]
        self.state["role_id"] = role_id
        principals = json.load(open(os.path.join(self.inputs, "team-principals.json")))
        code, d = self.api("GET", f"/namespaces/{self.state['ns']}/iam/access-bindings")
        rows = d.get("data") or []
        have = {(b.get("subjectId"), b.get("roleId"), b.get("resourceKind"), b.get("resourceId"))
                for b in rows if isinstance(b, dict)}
        exact_targets = ([("agent", self.state["agent"]),
                          ("configuration", self.state["configuration"])]
                         + [("credential_source", sid) for sid in self.state["sources"].values()])
        for user in self.spec["users"]:
            prn = principals[user["name"]]
            for kind, rid in exact_targets:
                if (prn, role_id, kind, rid) in have:
                    continue
                code, d = self.api("POST", f"/namespaces/{self.state['ns']}/iam/access-bindings",
                                   {"subjectKind": "identity", "subjectId": prn, "roleId": role_id,
                                    "resourceKind": kind, "resourceId": rid})
                assert code == "201", f"binding {user['name']} {kind}: {code} {json.dumps(d)[:200]}"
            if (prn, role_id, "namespace", self.state["ns"]) not in have:
                code, d = self.api("POST", f"/namespaces/{self.state['ns']}/iam/access-bindings",
                                   {"subjectKind": "identity", "subjectId": prn, "roleId": role_id,
                                    "resourceKind": "namespace", "resourceId": self.state["ns"]})
                assert code == "201", f"binding {user['name']} namespace: {code}"
            print(f"[binding] {user['name']}: exact agent/configuration/credential-sources + namespace")
        self.save_state()

    # ---------- stage 6 ----------

    def stage_summary(self):
        host = self.state.get("control_ui_host") or self.control_ui_host()
        print("\n=== ROLLOUT COMPLETE ===")
        print(f"namespace:   {self.spec['namespace']} ({self.state['ns']}, k8s {self.state['kns']})")
        print(f"agent:       {self.spec['agent']} ({self.state['agent']})")
        print(f"revision:    {self.state.get('revision')}")
        print(f"Control UI:  https://{host}/new  (OCC console sign-in FIRST, then this route)")
        print(f"users:       {', '.join(u['name'] for u in self.spec['users'])}"
              " — co-owners via namespace_owner; new-account passwords in team-accounts-passwords.json")

    # ---------- check ----------

    def run_check(self):
        v = self.live_values()
        print("LIVE environment reads (all values below are read at run time):")
        for k in ("cluster_domain", "kube_api_svc", "kube_api_endpoints", "dns_svc",
                  "dns_pod_ips", "oauth_svc", "oauth_pod_ips", "router_ip"):
            print(f"  {k}: {v[k]}")
        for name, img in v["images"].items():
            print(f"  image.{name}: {img}")
        print(f"  control-ui host will be: oc-ui-<gateway-suffix>.{v['cluster_domain']}")
        assert v["kube_api_svc"] != v["dns_svc"], "kube API and DNS service IPs identical?!"
        print("CHECK OK")

    # ---------- driver ----------

    def run(self):
        stages = [("namespace", self.stage_namespace), ("materials", self.stage_materials),
                  ("agent", self.stage_agent), ("origin", self.stage_origin),
                  ("controlui", self.stage_controlui), ("summary", self.stage_summary)]
        for index, (name, fn) in enumerate(stages, start=1):
            if index < self.from_stage:
                continue
            print(f"\n--- stage {index}: {name} ---", flush=True)
            fn()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--check", action="store_true", help="read-only environment validation")
    ap.add_argument("--from", dest="from_stage", type=int, default=1)
    args = ap.parse_args()
    run = Run(args.spec, check_only=args.check, from_stage=args.from_stage)
    if args.check:
        run.run_check()
        return 0
    run.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

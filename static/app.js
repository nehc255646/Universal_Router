function app() {
  return {
    providers: [],
    selectedId: null,
    selected: null,
    form: { id: '', display_name: '', base_url: '', api_key: '', inbound_key: '', use_env_key: false, upstream_mode: 'chat_completions', models: [], headers: [], enabled: true, priority: 100, weight: 1, timeout_s: 120, cost_input_per_1m: 0, cost_output_per_1m: 0 },
    showKey: false,
    showInboundKey: false,
    msg: '',
    msgOk: true,
    testResult: '',
    testing: false,
    healthOk: false,
    version: '',
    gatewayUrl: 'http://127.0.0.1:8787/v1',
    serverCfg: { host:'127.0.0.1', port:8787, local_api_key:'', admin_api_key:'', retry_count:1, retry_backoff_ms:200, failover:true, route_strategy:'priority', log_retain:5000, connect_timeout_s:15, first_token_timeout_s:45, read_idle_timeout_s:90, circuit_breaker:true, circuit_fail_threshold:3, circuit_cooldown_s:30, has_local_api_key:false, has_admin_api_key:false },
    adminKey: localStorage.getItem('ur_admin_key') || '',
    needAdmin: false,
    providerHealth: {},
    showLocalKey: false,
    tab: 'config',
    fetchingModels: false,
    logs: [],
    logTotal: 0,
    logOffset: 0,
    logLimit: 50,
    status: null,
    presets: [
      { id: 'openai', display_name: 'OpenAI', base_url: 'https://api.openai.com/v1', upstream_mode: 'chat_completions', models: [{id:'gpt-4o', display_name:'GPT-4o'}, {id:'gpt-4o-mini', display_name:'GPT-4o mini'}] },
      { id: 'anthropic', display_name: 'Anthropic', base_url: 'https://api.anthropic.com/v1', upstream_mode: 'messages', models: [{id:'claude-sonnet-4-5', display_name:'Claude Sonnet 4.5'}, {id:'claude-haiku-4-5', display_name:'Claude Haiku 4.5'}] },
      { id: 'openrouter', display_name: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1', upstream_mode: 'chat_completions', models: [{id:'openai/gpt-4o', display_name:'GPT-4o'}, {id:'anthropic/claude-sonnet-4.5', display_name:'Claude Sonnet 4.5'}] },
      { id: 'deepseek', display_name: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', upstream_mode: 'chat_completions', models: [{id:'deepseek-chat', display_name:'DeepSeek Chat'}, {id:'deepseek-reasoner', display_name:'DeepSeek Reasoner'}] },
    ],
    play: { model: '', protocol: 'chat', stream: true, tools: false, system: '', prompt: '', output: '', reasoning: '', loading: false, error: '' },

    async init() {
      await this.refresh();
      await this.loadServer();
      this.checkHealth();
      this.loadLogs();
      this.loadStatus();
      setInterval(() => this.checkHealth(), 10000);
    },
    adminHeaders() {
      const h = {'Content-Type':'application/json'};
      const k = (this.adminKey || '').trim();
      if (k) { h['Authorization'] = 'Bearer ' + k; h['X-Admin-Key'] = k; }
      return h;
    },
    async api(path, opts={}) {
      const r = await fetch(path, {...opts, headers:{...this.adminHeaders(), ...(opts.headers||{})}});
      if (r.status === 401 || r.status === 403) this.needAdmin = true;
      else if (r.ok) this.needAdmin = false;
      return r;
    },
    async unlockAdmin() {
      localStorage.setItem('ur_admin_key', this.adminKey || '');
      this.needAdmin = false;
      await this.refresh();
      await this.loadServer();
      await this.loadStatus();
    },
    healthOf(id) { return this.providerHealth[id] || null; },
    healthLabel(id) {
      const h = this.healthOf(id);
      if (!h) return '';
      const lat = h.ewma_latency_ms ? Math.round(h.ewma_latency_ms)+'ms' : '';
      return (h.state || 'closed') + (lat ? ' · '+lat : '');
    },
    healthBadgeClass(id) {
      const h = this.healthOf(id);
      if (!h) return 'bg-zinc-800 text-zinc-500';
      if (h.state === 'open') return 'bg-red-500/15 text-red-300';
      if (h.state === 'half_open') return 'bg-amber-500/15 text-amber-300';
      return 'bg-emerald-500/15 text-emerald-300';
    },
    async loadServer(){
      try{
        const r = await this.api('/api/config');
        const cfg = await r.json();
        if(cfg.server){ this.serverCfg = {...this.serverCfg, ...cfg.server}; this.gatewayUrl=`http://${cfg.server.host}:${cfg.server.port}/v1`; }
      }catch{}
    },
    async loadStatus(){
      try {
        const r = await this.api('/api/status');
        this.status = await r.json();
        const map = {};
        for (const h of (this.status && this.status.provider_health) || []) map[h.provider_id] = h;
        this.providerHealth = map;
      } catch { this.status = null; }
    },
    async saveServer(){
      const srv = {...this.serverCfg};
      if (!srv.local_api_key) delete srv.local_api_key;
      if (!srv.admin_api_key) delete srv.admin_api_key;
      const r=await this.api('/api/config',{method:'PUT',body:JSON.stringify({server:srv})});
      const d=await r.json();
      if(!r.ok){ this.toast(d.detail||JSON.stringify(d),false); return; }
      const typedAdmin = (srv.admin_api_key || srv.local_api_key || '').trim();
      if (typedAdmin) { this.adminKey = typedAdmin; localStorage.setItem('ur_admin_key', typedAdmin); }
      this.serverCfg={...this.serverCfg, ...d.server}; this.gatewayUrl=`http://${d.server.host}:${d.server.port}/v1`;
      this.toast('网关配置已保存，重启后生效',true);
    },
    async checkHealth() {
      try {
        const r = await fetch('/health');
        this.healthOk = r.ok;
        if (r.ok) {
          const d = await r.json();
          this.version = d.version || '';
        }
      } catch { this.healthOk = false; }
    },
    modeLabel(m) {
      return { chat_completions: 'chat', responses: 'responses', messages: 'messages' }[m] || m;
    },
    copy(t) {
      navigator.clipboard.writeText(t);
      this.toast('已复制', true);
    },
    toast(m, ok) {
      this.msg = m; this.msgOk = ok;
      setTimeout(()=> { if(this.msg===m) this.msg=''; }, 3000);
    },
    async refresh() {
      const r = await this.api('/api/providers');
      this.providers = await r.json();
      if (!Array.isArray(this.providers)) this.providers = [];
      if (this.selectedId) this.select(this.selectedId);
      if (!this.play.model) {
        const all = this.allModels();
        if (all.length) this.play.model = all[0].id;
      }
      this.loadStatus();
    },
    allModels() {
      const out = [];
      for (const p of this.providers) {
        for (const m of (p.models || [])) {
          out.push({ id: m.id, prefixed: p.id + '/' + m.id, label: (m.display_name || m.id) + ' · ' + p.id, provider: p.id });
        }
      }
      return out;
    },
    select(id) {
      this.selectedId = id;
      const p = this.providers.find(x=>x.id===id);
      if (!p) { this.selected=null; return; }
      this.selected = p;
      this.form = JSON.parse(JSON.stringify(p));
      this.form.use_env_key = !!p.api_key_is_ref;
      if (p.api_key_is_ref) this.form.api_key = this.envName(p.api_key);
      else this.form.api_key = '';
      this.form.inbound_key = p.inbound_key_is_ref ? p.inbound_key : '';
      this.msg=''; this.testResult='';
      this.tab = 'config';
    },
    envName(v) {
      v = (v || '').trim();
      if (v.toLowerCase().startsWith('env:')) return v.slice(4).trim();
      if (v.startsWith('${') && v.endsWith('}')) return v.slice(2, -1).trim();
      if (v.startsWith('$')) return v.slice(1);
      return v;
    },
    emptyForm() {
      return { id: '', display_name: '', base_url: '', api_key: '', inbound_key: '', use_env_key: false, has_api_key: false, has_inbound_key: false, api_key_is_ref: false, inbound_key_is_ref: false, upstream_mode: 'chat_completions', models: [{id:'',display_name:'',upstream_id:''}], headers: [], enabled: true, priority: 100, weight: 1, timeout_s: 120, cost_input_per_1m: 0, cost_output_per_1m: 0 };
    },
    onToggleEnvKey() {
      if (this.form.use_env_key) {
        const v = (this.form.api_key || '').trim();
        if (!v) return;
        if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(v) || v.toLowerCase().startsWith('env:') || v.startsWith('$')) {
          this.form.api_key = this.envName(v);
        } else {
          this.form.api_key = '';
        }
      } else if (this.form.api_key_is_ref) {
        this.form.api_key = '';
      }
    },
    addProvider() {
      this.selectedId = null;
      this.selected = { id: '' };
      this.form = this.emptyForm();
      this.msg=''; this.testResult='';
      this.tab = 'config';
    },
    applyPreset(pr) {
      this.form.id = this.form.id || pr.id;
      this.form.display_name = this.form.display_name || pr.display_name;
      this.form.base_url = pr.base_url;
      this.form.upstream_mode = pr.upstream_mode;
      if (!this.form.models || !this.form.models.filter(m=>m.id).length) {
        this.form.models = JSON.parse(JSON.stringify(pr.models));
      }
    },
    async save() {
      const isNew = !this.providers.find(x=>x.id===this.selectedId);
      let url, method;
      if (isNew || !this.selectedId) {
        url = '/api/providers'; method='POST';
      } else {
        url = `/api/providers/${this.selectedId}`; method='PUT';
      }
      const body = {...this.form, models: this.form.models.filter(m=>m.id && m.id.trim()), headers: this.form.headers.filter(h=>h.name && h.name.trim())};
      if (body.use_env_key) body.api_key_from_env = true;
      delete body.use_env_key;
      if (!body.api_key) delete body.api_key;
      if (!body.inbound_key) delete body.inbound_key;
      const r = await this.api(url, {method, body: JSON.stringify(body)});
      const data = await r.json();
      if (!r.ok) { this.toast(data.detail || JSON.stringify(data), false); return; }
      this.toast('保存成功', true);
      await this.refresh();
      this.select(data.id);
    },
    providerSaveBody(extra={}) {
      const body = {...this.form, models: this.form.models.filter(m=>m.id && m.id.trim()), headers: this.form.headers.filter(h=>h.name && h.name.trim()), ...extra};
      delete body.use_env_key;
      return body;
    },
    async clearKey() {
      if (!this.selectedId) { this.form.api_key=''; this.form.has_api_key=false; this.form.use_env_key=false; return; }
      const body = this.providerSaveBody({ api_key:'', clear_api_key:true });
      const r = await this.api(`/api/providers/${this.selectedId}`, {method:'PUT', body: JSON.stringify(body)});
      const data = await r.json();
      if (!r.ok) { this.toast(data.detail || JSON.stringify(data), false); return; }
      this.toast('密钥已清除', true);
      await this.refresh();
      this.select(data.id);
    },
    async clearInboundKey() {
      if (!this.selectedId) { this.form.inbound_key=''; this.form.has_inbound_key=false; return; }
      const body = this.providerSaveBody({ inbound_key:'', clear_inbound_key:true });
      const r = await this.api(`/api/providers/${this.selectedId}`, {method:'PUT', body: JSON.stringify(body)});
      const data = await r.json();
      if (!r.ok) { this.toast(data.detail || JSON.stringify(data), false); return; }
      this.toast('已恢复为上游密钥', true);
      await this.refresh();
      this.select(data.id);
    },
    async remove() {
      if (!this.selectedId || !confirm(`删除 ${this.selectedId} ?`)) return;
      const r = await this.api(`/api/providers/${this.selectedId}`, {method:'DELETE'});
      if (!r.ok) { this.toast('删除失败', false); return; }
      this.selectedId=null; this.selected=null;
      await this.refresh();
    },
    async testConn() {
      if (!this.selectedId) { this.toast('请先保存', false); return; }
      this.testing=true; this.testResult='';
      try {
        const r = await this.api(`/api/providers/${this.selectedId}/test`, {method:'POST'});
        const data = await r.json();
        this.testResult = JSON.stringify(data, null, 2);
        this.toast(data.ok ? `连通 ${data.latency_ms}ms` : '测试失败', !!data.ok);
      } catch(e) { this.testResult=String(e); }
      this.testing=false;
    },
    async fetchModels() {
      if (!this.selectedId) { this.toast('请先保存提供商', false); return; }
      this.fetchingModels = true;
      try {
        const r = await this.api(`/api/providers/${this.selectedId}/models/fetch`, {method:'POST'});
        const data = await r.json();
        if (!r.ok || !data.ok) { this.toast((data.error && JSON.stringify(data.error)) || '拉取失败', false); return; }
        const existing = new Set(this.form.models.map(m=>m.id));
        for (const m of data.models) {
          if (!existing.has(m.id)) this.form.models.push({id: m.id, display_name: m.display_name || m.id, upstream_id: ''});
        }
        this.toast(`已合并 ${data.models.length} 个上游模型`, true);
      } catch(e) { this.toast(String(e), false); }
      this.fetchingModels = false;
    },
    async loadLogs() {
      try {
        const data = await this.api(`/api/logs?limit=${this.logLimit}&offset=${this.logOffset}`).then(x=>x.json());
        if (Array.isArray(data)) { this.logs = data; this.logTotal = data.length; }
        else { this.logs = data.items || []; this.logTotal = data.total || 0; }
      } catch { this.logs = []; }
    },
    async clearLogs() {
      await this.api('/api/logs', {method:'DELETE'});
      this.logs = []; this.logTotal = 0; this.logOffset = 0;
    },
    async logPrev() {
      this.logOffset = Math.max(0, this.logOffset - this.logLimit);
      await this.loadLogs();
    },
    async logNext() {
      if (this.logOffset + this.logLimit >= this.logTotal) return;
      this.logOffset += this.logLimit;
      await this.loadLogs();
    },
    fmtTime(ts) {
      const d = new Date(ts * 1000);
      return d.toLocaleTimeString();
    },
    curlSnippet() {
      const model = this.play.model || (this.allModels()[0] && this.allModels()[0].id) || 'gpt-4o';
      const key = (this.serverCfg.local_api_key || '').trim() || 'sk-local';
      const url = this.gatewayUrl.replace(/\/$/, '');
      return `curl ${url}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${key}" \\
  -d '{"model":"${model}","messages":[{"role":"user","content":"你好"}]}'`;
    },
    pythonSnippet() {
      const model = this.play.model || 'gpt-4o';
      const key = (this.serverCfg.local_api_key || '').trim() || 'sk-local';
      const url = this.gatewayUrl.replace(/\/$/, '');
      return `from openai import OpenAI
client = OpenAI(base_url="${url}", api_key="${key}")
r = client.chat.completions.create(model="${model}", messages=[{"role":"user","content":"你好"}])
print(r.choices[0].message.content)`;
    },
    async sendPlay() {
      if (!this.play.prompt.trim()) { this.toast('请输入内容', false); return; }
      if (!this.play.model) { this.toast('请选择模型', false); return; }
      this.play.loading = true; this.play.output = ''; this.play.reasoning = ''; this.play.error = '';
      const messages = [];
      if (this.play.system.trim()) messages.push({role:'system', content: this.play.system.trim()});
      messages.push({role:'user', content: this.play.prompt.trim()});
      const sampleTool = {
        name: 'get_weather',
        description: 'Get weather for a city',
        parameters: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] },
      };
      let path = '/api/play/chat';
      let body = { model: this.play.model, messages, stream: this.play.stream };
      if (this.play.tools) body.tools = [{ type: 'function', function: sampleTool }];
      if (this.play.protocol === 'responses') {
        path = '/api/play/responses';
        body = { model: this.play.model, input: messages, stream: this.play.stream };
        if (this.play.system.trim()) body.instructions = this.play.system.trim();
        if (this.play.tools) body.tools = [{ type: 'function', name: sampleTool.name, description: sampleTool.description, parameters: sampleTool.parameters }];
      } else if (this.play.protocol === 'messages') {
        path = '/api/play/messages';
        const sys = messages.filter(m=>m.role==='system').map(m=>m.content).join('\n');
        body = { model: this.play.model, messages: messages.filter(m=>m.role!=='system'), max_tokens: 1024, stream: this.play.stream };
        if (sys) body.system = sys;
        if (this.play.tools) body.tools = [{ name: sampleTool.name, description: sampleTool.description, input_schema: sampleTool.parameters }];
      }
      try {
        const r = await this.api(path, { method:'POST', body: JSON.stringify(body) });
        if (!this.play.stream) {
          const data = await r.json();
          if (!r.ok) { this.play.error = JSON.stringify(data, null, 2); this.play.loading=false; return; }
          this.play.output = this.extractText(data, this.play.protocol);
          this.play.reasoning = this.extractReasoningFull(data, this.play.protocol);
          this.play.loading=false;
          this.loadLogs();
          return;
        }
        if (!r.ok) {
          const t = await r.text();
          this.play.error = t;
          this.play.loading=false;
          return;
        }
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          buf += dec.decode(value, {stream:true});
          const parts = buf.split('\n\n');
          buf = parts.pop() || '';
          for (const part of parts) {
            for (const line of part.split('\n')) {
              const s = line.trim();
              if (!s.startsWith('data:')) continue;
              const payload = s.slice(5).trim();
              if (payload === '[DONE]') continue;
              try {
                const ev = JSON.parse(payload);
                if (ev.error) { this.play.error = JSON.stringify(ev.error); continue; }
                const d = this.extractDelta(ev, this.play.protocol);
                if (d) this.play.output += d;
                const th = this.extractReasoning(ev, this.play.protocol);
                if (th) this.play.reasoning += th;
              } catch {}
            }
          }
        }
      } catch(e) {
        this.play.error = String(e);
      }
      this.play.loading = false;
      this.loadLogs();
    },
    extractReasoningFull(data, proto) {
      if (proto === 'chat') return (((data.choices||[])[0]||{}).message||{}).reasoning_content || '';
      if (proto === 'responses') {
        const item = (data.output||[]).find(x=>x.type==='reasoning');
        if (!item) return '';
        return ((item.summary||[]).map(s=>s.text||'').join('')) || '';
      }
      if (proto === 'messages') return (data.content||[]).filter(c=>c.type==='thinking').map(c=>c.thinking||'').join('');
      return '';
    },
    extractText(data, proto) {
      if (proto === 'chat') return (((data.choices||[])[0]||{}).message||{}).content || '';
      if (proto === 'responses') {
        const out = data.output || [];
        let t = '';
        for (const item of out) {
          for (const c of (item.content||[])) if (c.type==='output_text') t += c.text||'';
        }
        return t;
      }
      if (proto === 'messages') {
        return (data.content||[]).filter(c=>c.type==='text').map(c=>c.text||'').join('');
      }
      return JSON.stringify(data, null, 2);
    },
    extractDelta(ev, proto) {
      if (proto === 'chat') return (((ev.choices||[])[0]||{}).delta||{}).content || '';
      if (proto === 'responses' && ev.type === 'response.output_text.delta') return ev.delta || '';
      if (proto === 'messages' && ev.type === 'content_block_delta' && ev.delta && ev.delta.type==='text_delta') return ev.delta.text || '';
      return '';
    },
    extractReasoning(ev, proto) {
      if (proto === 'chat') return (((ev.choices||[])[0]||{}).delta||{}).reasoning_content || '';
      if (proto === 'responses' && (ev.type === 'response.reasoning_summary_text.delta' || ev.type === 'response.reasoning_text.delta')) return ev.delta || '';
      if (proto === 'messages' && ev.type === 'content_block_delta' && ev.delta && ev.delta.type==='thinking_delta') return ev.delta.thinking || ev.delta.text || '';
      return '';
    }
  }
}

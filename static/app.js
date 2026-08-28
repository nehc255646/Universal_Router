function app() {
  return {
    providers: [],
    selectedId: null,
    selected: null,
    form: { id: '', display_name: '', base_url: '', api_key: '', upstream_mode: 'chat_completions', models: [], headers: [] },
    showKey: false,
    msg: '',
    msgOk: true,
    testResult: '',
    testing: false,
    healthOk: false,
    gatewayUrl: 'http://127.0.0.1:8787/v1',
    serverCfg: { host:'127.0.0.1', port:8787, local_api_key:'' },
    showLocalKey: false,

    async init() {
      await this.refresh();
      await this.loadServer();
      this.checkHealth();
      setInterval(() => this.checkHealth(), 10000);
    },
    async loadServer(){
      try{
        const cfg = await fetch('/api/config').then(x=>x.json());
        if(cfg.server){ this.serverCfg = cfg.server; this.gatewayUrl=`http://${cfg.server.host}:${cfg.server.port}/v1`; }
      }catch{}
    },
    async saveServer(){
      const r=await fetch('/api/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({server:this.serverCfg})});
      const d=await r.json();
      if(!r.ok){ this.toast(d.detail||JSON.stringify(d),false); return; }
      this.serverCfg=d.server; this.gatewayUrl=`http://${d.server.host}:${d.server.port}/v1`;
      this.toast('网关配置已保存，重启后生效',true);
    },
    async checkHealth() {
      try {
        const r = await fetch('/health');
        this.healthOk = r.ok;
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
      setTimeout(()=> this.msg='', 3000);
    },
    async refresh() {
      const r = await fetch('/api/providers');
      this.providers = await r.json();
      if (this.selectedId) this.select(this.selectedId);
    },
    select(id) {
      this.selectedId = id;
      const p = this.providers.find(x=>x.id===id);
      if (!p) { this.selected=null; return; }
      this.selected = p;
      this.form = JSON.parse(JSON.stringify(p));
      this.msg=''; this.testResult='';
    },
    addProvider() {
      this.selectedId = null;
      this.selected = { id: '' };
      this.form = { id: '', display_name: '', base_url: '', api_key: '', upstream_mode: 'chat_completions', models: [{id:'',display_name:''}], headers: [] };
      this.msg=''; this.testResult='';
    },
    async save() {
      const isNew = !this.providers.find(x=>x.id===this.selectedId);
      let url, method;
      if (isNew || !this.selectedId) {
        url = '/api/providers'; method='POST';
      } else {
        url = `/api/providers/${this.selectedId}`; method='PUT';
      }
      // clean empty
      const body = {...this.form, models: this.form.models.filter(m=>m.id.trim()), headers: this.form.headers.filter(h=>h.name.trim())};
      const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const data = await r.json();
      if (!r.ok) { this.toast(data.detail || JSON.stringify(data), false); return; }
      this.toast('保存成功', true);
      await this.refresh();
      this.select(data.id);
    },
    async remove() {
      if (!this.selectedId || !confirm(`删除 ${this.selectedId} ?`)) return;
      const r = await fetch(`/api/providers/${this.selectedId}`, {method:'DELETE'});
      if (!r.ok) { this.toast('删除失败', false); return; }
      this.selectedId=null; this.selected=null;
      await this.refresh();
    },
    async testConn() {
      if (!this.selectedId) { this.toast('请先保存', false); return; }
      this.testing=true; this.testResult='';
      try {
        const r = await fetch(`/api/providers/${this.selectedId}/test`, {method:'POST'});
        const data = await r.json();
        this.testResult = JSON.stringify(data, null, 2);
        this.toast(data.ok ? `连通 ${data.latency_ms}ms` : '测试失败', !!data.ok);
      } catch(e) { this.testResult=String(e); }
      this.testing=false;
    }
  }
}

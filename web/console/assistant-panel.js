/* Contextual "Ask AI" (shared): floating button + right drawer that stays ON
   the current page — the drawer chat mirrors the studio assistant (bubbles:
   user right / agent left, Enter to send, model + thinking selects).
   Pages include this script and optionally pre-set context on row clicks:
     StudioAI.context = { agent_id: "abc123", ... };
   Port of f-ecommerce assistant-panel.js (#437): e-commerce scenario chips
   and SKU/platform parsers are generalized to agent-authoring scenarios;
   the built-in path is one-shot chat over the studio LLM gateway. */

const StudioAI = {
    context: {},

    chipSuggestions() {
        const c = this.context || {};
        const page = document.body.dataset.page || 'agents';
        const agent = c.agent_id || 'an agent';
        // Page-specific, English-only scenario chips.
        const byPage = {
            agents: [
                `What is the difference between base and tuned?`,
                `Draft an agent that lists the workspace agents every morning`,
                `Explain the approval workbench for ⚡ tools`,
                `Which capabilities are read-only?`,
            ],
        };
        if (c.agent_id) {
            byPage.agents = [
                `Suggest a system prompt for ${agent}`,
                `What tools would fit ${agent}?`,
                `Review the YAML of ${agent} for issues`,
            ];
        }
        return byPage[page] || byPage.agents;
    },

    esc(s) {
        return String(s ?? '').replace(
            /[&<>"']/g,
            (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
        );
    },

    ctxBadge() {
        const page = document.body.dataset.page || 'console';
        const parts = Object.entries(this.context || {})
            .filter(([, v]) => v !== undefined && v !== null && v !== '')
            .map(([k, v]) => `${k}=${v}`);
        return parts.length ? `${page} · ${parts.join(', ')}` : page;
    },

    fullPageUrl() {
        // full-page assistant = the studio web app chat
        return '/#chat';
    },

    open() {
        document.getElementById('ai-drawer').classList.add('open');
        this.renderChips();
        document.getElementById('ai-ctx').textContent = this.ctxBadge();
        document.getElementById('ai-input').focus();
    },
    close() {
        document.getElementById('ai-drawer').classList.remove('open');
    },

    renderChips() {
        const box = document.getElementById('ai-chips');
        box.innerHTML = this.chipSuggestions()
            .map((s) => `<span class="chip" data-fill="${this.esc(s)}">${this.esc(s)}</span>`)
            .join('');
    },

    fill(text) {
        const input = document.getElementById('ai-input');
        input.value = text;
        this.autoGrow();
        this.run();
    },
    autoGrow() {
        const input = document.getElementById('ai-input');
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    },

    addUserMsg(text) {
        const chat = document.getElementById('ai-chat');
        const div = document.createElement('div');
        div.className = 'msg-user';
        div.textContent = text;
        chat.appendChild(div);
        this.scrollDown();
    },
    addAgentMsg(html) {
        const chat = document.getElementById('ai-chat');
        const div = document.createElement('div');
        div.className = 'msg-agent';
        div.innerHTML = html;
        chat.appendChild(div);
        this.scrollDown();
        return div;
    },
    scrollDown() {
        const chat = document.getElementById('ai-chat');
        chat.scrollTop = chat.scrollHeight;
    },

    async runCustomerAgent(agentId, instruction) {
        const pending = this.addAgentMsg('<span class="muted">Agent running…</span>');
        try {
            const r = await fetch(`/v1/agents-platform/agents/${agentId}/test`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: instruction }),
            });
            const data = await r.json();
            if (!r.ok) {
                pending.innerHTML = `<span class="err">${this.esc(data.detail || 'run failed')}</span>`;
                this.scrollDown();
                return;
            }
            let html = '';
            for (const ev of data.events || []) {
                const t = ev.type || '';
                const c = String(ev.content || '');
                if (t === 'start' || t === 'safety_approval') continue;
                if (t === 'think') {
                    html +=
                        `<div class="step AUTO"><span class="v">THINK</span> ` +
                        `<span class="muted">${this.esc(c.slice(0, 140))}</span></div>`;
                } else if (t === 'tool_call' || t === 'tool_result') {
                    html +=
                        `<div class="step RUNNING"><span class="v">` +
                        `${t === 'tool_call' ? 'TOOL' : 'RESULT'}</span> ` +
                        `<span class="muted">${this.esc(c.slice(0, 180))}</span></div>`;
                } else if (t === 'error') {
                    html +=
                        `<div class="step REJECTED"><span class="v">ERROR</span> ` +
                        `${this.esc(c.slice(0, 220))}</div>`;
                } else if (c) {
                    html += `<div>${this.esc(c)}</div>`;
                }
            }
            pending.innerHTML = html || '<span class="muted">(no output)</span>';
        } catch (e) {
            pending.innerHTML = `<span class="err">${this.esc(String(e))}</span>`;
        }
        this.scrollDown();
    },

    async run() {
        const input = document.getElementById('ai-input');
        const instruction = input.value.trim();
        if (!instruction) return;
        this.addUserMsg(instruction);
        input.value = '';
        this.autoGrow();
        // Customer-agent branch: a published agent runs through its FAS
        // definition via the test bench; the built-in assistant path below is
        // the default.
        const agentSel = document.getElementById('ai-agent');
        if (agentSel && agentSel.value) {
            await this.runCustomerAgent(agentSel.value, instruction);
            return;
        }
        const pending = this.addAgentMsg('<span class="muted">Agent running…</span>');
        try {
            const r = await fetch('/v1/agents-platform/assistant', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: instruction,
                    context: this.context || {},
                    model: document.getElementById('ai-model').value,
                    thinking: document.getElementById('ai-thinking').value,
                }),
            });
            const data = await r.json();
            if (!r.ok) {
                pending.innerHTML = `<span class="err">${this.esc(data.detail || data.error || 'run failed')}</span>`;
            } else {
                pending.innerHTML = `<div>${this.esc(data.reply || '(no output)')}</div>`;
            }
        } catch (e) {
            pending.innerHTML = `<span class="err">${this.esc(String(e))}</span>`;
        }
        this.scrollDown();
    },

    renderYamlImport(d) {
        let html =
            `<div><b>YAML check:</b> <span class="v">${this.esc(d.ok ? 'compiles' : 'failed')}</span>` +
            (d.warnings && d.warnings.length
                ? `, warnings: ${this.esc(d.warnings.join('; '))}`
                : '') +
            `</div>`;
        if (!d.ok && d.error) html += `<div class="step FAILED">${this.esc(d.error)}</div>`;
        else if (d.ok)
            html += `<div class="muted">Open the editor, paste the YAML in the YAML tab and "Compile & sync form".</div>`;
        return html;
    },

    async uploadYaml(file) {
        this.addUserMsg(`📄 Upload YAML: ${file.name}`);
        const pending = this.addAgentMsg('<span class="muted">Validating…</span>');
        try {
            const text = await file.text();
            const r = await fetch('/v1/agents-platform/compile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ yaml: text }),
            });
            const data = await r.json();
            pending.innerHTML = r.ok
                ? this.renderYamlImport(data)
                : `<span class="err">${this.esc(data.detail || data.error || 'validation failed')}</span>`;
        } catch (e) {
            pending.innerHTML = `<span class="err">${this.esc(String(e))}</span>`;
        }
        this.scrollDown();
    },

    mount() {
        const fab = document.createElement('button');
        fab.className = 'ai-fab';
        fab.textContent = '✦ Ask AI';
        fab.onclick = () => this.open();

        const drawer = document.createElement('div');
        drawer.id = 'ai-drawer';
        drawer.className = 'ai-drawer';
        drawer.innerHTML =
            `<div class="ai-head"><b>✦ AI Assistant</b>` +
            `<span class="badge" id="ai-ctx"></span>` +
            `<a href="#" id="ai-open-full" title="Open the full-page assistant with this context">Open ↗</a>` +
            `<button onclick="StudioAI.close()">✕</button></div>` +
            `<div class="ai-agent-row"><label>Agent</label>` +
            `<select id="ai-agent" title="Run a published customer agent instead of the built-in assistant">` +
            `<option value="">Built-in assistant</option>` +
            `</select></div>` +
            `<div class="ai-chat" id="ai-chat">` +
            `<div class="ai-empty">Ask about this page — context is carried automatically. ` +
            `You right, agent left. <kbd>Enter</kbd> sends.</div>` +
            `</div>` +
            `<div class="ai-chips" id="ai-chips"></div>` +
            `<div class="composer-box">` +
            `<textarea id="ai-input" placeholder="Ask in Chinese / English…"></textarea>` +
            `<div class="composer-row">` +
            `<div class="plus-wrap">` +
            `<button class="plus-btn" id="ai-plus" title="Actions">+</button>` +
            `<div class="popover" id="ai-plus-popover">` +
            `<div class="po-title">ACTIONS</div>` +
            `<span class="chip" id="ai-upload-chip">📄 Upload agent YAML — validate before editing</span>` +
            `</div></div>` +
            `<span class="spacer"></span>` +
            `<select id="ai-model" title="Model">` +
            `<option value="base">base</option>` +
            `</select>` +
            `<select id="ai-thinking" title="Thinking level">` +
            `<option value="auto">auto</option>` +
            `<option value="low">low</option>` +
            `<option value="high">high</option>` +
            `<option value="xhigh">xhigh</option>` +
            `</select>` +
            `<button class="primary" onclick="StudioAI.run()">▶</button>` +
            `</div></div>` +
            `<input type="file" id="ai-yaml-file" accept=".yaml,.yml,text/yaml" class="hidden-file">`;

        document.body.appendChild(fab);
        document.body.appendChild(drawer);

        const input = document.getElementById('ai-input');
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.run();
            }
        });
        input.addEventListener('input', () => this.autoGrow());
        // chips are re-rendered — delegate clicks on the container
        document.getElementById('ai-chips').addEventListener('click', (e) => {
            const chip = e.target.closest('.chip[data-fill]');
            if (chip) this.fill(chip.dataset.fill);
        });
        document.getElementById('ai-open-full').addEventListener('click', (e) => {
            e.preventDefault();
            location.href = this.fullPageUrl();
        });
        // + actions: YAML upload (file picker → compile check → system bubble)
        const popover = document.getElementById('ai-plus-popover');
        document.getElementById('ai-plus').addEventListener('click', (e) => {
            e.stopPropagation();
            popover.classList.toggle('open');
        });
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.plus-wrap')) popover.classList.remove('open');
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') popover.classList.remove('open');
        });
        const yamlInput = document.getElementById('ai-yaml-file');
        document.getElementById('ai-upload-chip').addEventListener('click', () => {
            popover.classList.remove('open');
            yamlInput.click();
        });
        yamlInput.addEventListener('change', async () => {
            const file = yamlInput.files[0];
            yamlInput.value = ''; // re-uploading the same file retriggers
            if (file) await this.uploadYaml(file);
        });
        // model-selector honesty (LLM option off → disabled) + tuned registry
        fetch('/v1/agents-platform/agent-descriptor')
            .then((r) => r.json())
            .then((d) => {
                const sel = document.getElementById('ai-model');
                if (!sel) return;
                if (!d.llm_workflows_enabled) {
                    sel.disabled = true;
                    sel.title = 'no default model configured on the gateway';
                }
                if (d.tuned_model) {
                    let t = sel.querySelector('option[value="tuned"]');
                    if (!t) {
                        t = document.createElement('option');
                        t.value = 'tuned';
                        sel.appendChild(t);
                    }
                    t.textContent = d.tuned_model.label || 'tuned';
                }
            })
            .catch(() => {});
        // agent picker: published customer agents (drafts never appear here)
        fetch('/v1/agents-platform/agents')
            .then((r) => r.json())
            .then((d) => {
                const sel = document.getElementById('ai-agent');
                if (!sel) return;
                const published = (d.agents || []).filter((a) => a.status === 'published');
                for (const a of published) {
                    const opt = document.createElement('option');
                    opt.value = a.id;
                    opt.textContent = `${a.name} (v${a.version ?? a.version_int ?? 1})`;
                    sel.appendChild(opt);
                }
                if (!published.length) {
                    sel.disabled = true;
                    sel.title = 'No published agents yet — build one on the Agents page';
                }
            })
            .catch(() => {});
        this.renderChips();
    },
};

window.StudioAI = StudioAI;
document.addEventListener('DOMContentLoaded', () => StudioAI.mount());

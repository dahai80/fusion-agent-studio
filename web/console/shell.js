/* Shared shell: injects sidebar + topbar into every console page.
   Pages declare <body data-page="agents"> and call shell.init().
   Port of f-ecommerce shell.js (#437): the menu list still comes from the
   server (/v1/agents-platform/whoami → menus) — no hardcoded roles here. */

const NAV_FALLBACK = [{ id: 'agents', label: 'Agents', href: 'agents.html' }];

const shell = {
    async init() {
        let me = { user: '', role: '', menus: [] };
        try {
            me = await fetch('/v1/agents-platform/whoami').then((r) => r.json());
        } catch (_) {
            /* API down — fall back to NAV_FALLBACK below */
        }
        const role = me.role || 'admin';
        const page = document.body.dataset.page || '';
        const menus = Array.isArray(me.menus) && me.menus.length ? me.menus : NAV_FALLBACK;
        const aside = document.createElement('aside');
        aside.className = 'sidebar';
        aside.innerHTML =
            `<div class="brand">Fusion Agent Studio</div>` +
            menus
                .map(
                    (n) =>
                        `<a href="${n.href}" class="${n.id === page ? 'active' : ''}">${n.label}</a>`
                )
                .join('') +
            `<div class="spacer"></div>` +
            `<div class="foot">Signed in as <b>${me.user || '(local)'}</b><br>role: ${role}<br>` +
            `<a href="#" id="theme-toggle">theme</a></div>`;
        const layout = document.createElement('div');
        layout.className = 'layout';
        while (document.body.firstChild) layout.appendChild(document.body.firstChild);
        const main = document.createElement('div');
        main.className = 'main';
        while (layout.firstChild && layout.firstChild !== aside) {
            // move original body content into <main> (after sidebar placeholder)
            main.appendChild(layout.firstChild);
        }
        layout.appendChild(aside);
        layout.appendChild(main);
        document.body.appendChild(layout);

        // topbar: title + role badge
        const h1 = main.querySelector('h1');
        if (h1) {
            const bar = document.createElement('div');
            bar.className = 'topbar';
            bar.appendChild(h1);
            const badge = document.createElement('span');
            badge.className = 'badge ' + (role === 'auditor' ? 'info' : 'success');
            badge.textContent = role;
            bar.appendChild(badge);
            main.insertBefore(bar, main.children[0].nextSibling || null);
        }
        const toggle = document.getElementById('theme-toggle');
        if (toggle)
            toggle.onclick = (e) => {
                e.preventDefault();
                document.documentElement.classList.toggle('light');
                document.cookie =
                    'studio_theme=' +
                    (document.documentElement.classList.contains('light') ? 'light' : 'dark') +
                    '; path=/';
            };
        if (document.cookie.includes('studio_theme=light'))
            document.documentElement.classList.add('light');
        this.role = role;
    },
};

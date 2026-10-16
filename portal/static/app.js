(function () {
  "use strict";

  var ICONS = {
    box: "M21 8l-9-5-9 5v8l9 5 9-5V8z M3 8l9 5 9-5 M12 13v8",
    server: "M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01",
    network: "M12 3v5 M12 8H5v5 M12 8h7v5 M5 13v1 M19 13v1 M3 14h4v4H3z M17 14h4v4h-4z M10 14h4v4h-4z M12 8v6",
    globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M3 12h18 M12 3c3 3 3 15 0 18 M12 3c-3 3-3 15 0 18",
    shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z",
    terminal: "M4 5h16v14H4z M7 9l3 3-3 3 M12 15h5",
    git: "M6 3v12 M6 18a3 3 0 1 0 0 .01 M18 9a3 3 0 1 0 0 .01 M18 12c0 4-5 4-12 3",
    play: "M7 4l13 8-13 8V4z",
    home: "M3 11l9-8 9 8 M5 10v10h5v-6h4v6h5V10",
    menu: "M4 6h16 M4 12h16 M4 18h16",
    x: "M6 6l12 12 M18 6L6 18",
    external: "M14 4h6v6 M20 4l-9 9 M18 14v5H5V6h5",
    logout: "M10 4H5v16h5 M15 8l4 4-4 4 M19 12H9",
    cert: "M12 3l2 3 3 .5-2 2.5.5 3-3.5-1.5L8.5 12l.5-3L7 6.5 10 6l2-3z M9 14l-1 7 4-2 4 2-1-7",
    user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4 21c0-4 4-6 8-6s8 2 8 6",
    download: "M12 3v12 M7 11l5 5 5-5 M5 21h14",
    clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v5l3 2",
    sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z M12 2v2 M12 20v2 M4.9 4.9l1.4 1.4 M17.7 17.7l1.4 1.4 M2 12h2 M20 12h2 M4.9 19.1l1.4-1.4 M17.7 6.3l1.4-1.4",
    moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"
  };
  var SVG_NS = "http://www.w3.org/2000/svg";

  function icon(name) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[name] || ICONS.box);
    svg.appendChild(path);
    return svg;
  }

  function el(tag, props, children) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (key) {
      if (key === "text") node.textContent = props[key];
      else if (key === "class") node.className = props[key];
      else node.setAttribute(key, props[key]);
    });
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function dot(name) { return el("span", { "class": "dot", "data-dot": name }); }

  var app = document.getElementById("app");
  var drawer = document.getElementById("drawer");
  var tabsBar = document.getElementById("tabs");
  var stage = document.getElementById("stage");
  var home = document.getElementById("home");
  var externalBtn = document.getElementById("open-external");
  var toggleBtn = document.getElementById("drawer-toggle");
  var hasCa = document.body.getAttribute("data-has-ca") === "true";

  var services = [];
  var byName = {};
  var opened = {};        // nom -> { tab, frame }
  var order = [];       // noms des onglets ouverts, dans l'ordre
  var active = "home";

  // --- theme clair / sombre (sombre par defaut, choix memorise) ---
  var themeBtn = document.getElementById("theme-toggle");
  function renderThemeButton() {
    var dark = document.documentElement.getAttribute("data-theme") !== "light";
    themeBtn.textContent = "";
    themeBtn.appendChild(icon(dark ? "sun" : "moon"));  // l'icone montre ce vers quoi on bascule
    themeBtn.setAttribute("aria-pressed", String(!dark));
  }
  themeBtn.addEventListener("click", function () {
    var next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("expolab.theme", next); } catch (e) { /* ignore */ }
    renderThemeButton();
  });
  renderThemeButton();

  toggleBtn.appendChild(icon("menu"));
  externalBtn.appendChild(icon("external"));
  document.getElementById("logout-btn").appendChild(icon("logout"));

  // --- tiroir replie / deplie (memorise, replie par defaut sur petit ecran) ---
  function storedCollapsed() {
    try { var v = localStorage.getItem("expolab.drawer"); if (v !== null) return v === "1"; } catch (e) { /* stockage indisponible */ }
    return window.matchMedia("(max-width: 760px)").matches;
  }
  function setCollapsed(value) {
    app.classList.toggle("collapsed", value);
    toggleBtn.setAttribute("aria-expanded", String(!value));
    try { localStorage.setItem("expolab.drawer", value ? "1" : "0"); } catch (e) { /* ignore */ }
  }
  setCollapsed(storedCollapsed());
  toggleBtn.addEventListener("click", function () { setCollapsed(!app.classList.contains("collapsed")); });
  document.getElementById("scrim").addEventListener("click", function () { setCollapsed(true); });

  // --- rendu du tiroir et de l'accueil ---
  function groups() {
    var seen = [];
    var map = {};
    services.forEach(function (s) {
      if (!map[s.group]) { map[s.group] = []; seen.push(s.group); }
      map[s.group].push(s);
    });
    return seen.map(function (g) { return { title: g, items: map[g] }; });
  }

  function renderDrawer() {
    drawer.textContent = "";
    var homeItem = el("button", { "class": "nav-item", type: "button", "data-nav": "home" }, [icon("home"), el("span", { "class": "label", text: "Accueil" })]);
    homeItem.addEventListener("click", function () { activate("home"); });
    drawer.appendChild(homeItem);
    groups().forEach(function (group) {
      drawer.appendChild(el("div", { "class": "group-title", text: group.title }));
      group.items.forEach(function (s) {
        var item = el("button", { "class": "nav-item", type: "button", "data-nav": s.name, title: s.title }, [icon(s.icon), el("span", { "class": "label", text: s.title }), dot(s.name)]);
        item.addEventListener("click", function () { openService(s.name); if (window.matchMedia("(max-width: 760px)").matches) setCollapsed(true); });
        drawer.appendChild(item);
      });
    });
    if (byName.caddy) {
      drawer.appendChild(el("div", { "class": "group-title", text: "Compte" }));
      var account = el("button", { "class": "nav-item", type: "button", "data-nav": "compte", title: "Changer l'identifiant et le mot de passe" }, [icon("user"), el("span", { "class": "label", text: "Identifiants" })]);
      account.addEventListener("click", function () { openAccount(); if (window.matchMedia("(max-width: 760px)").matches) setCollapsed(true); });
      drawer.appendChild(account);
    }
    if (hasCa) {
      drawer.appendChild(el("div", { "class": "group-title", text: "Aide" }));
      var cert = el("a", { "class": "nav-item", href: "/ca.crt", title: "Télécharger le certificat racine" }, [icon("cert"), el("span", { "class": "label", text: "Certificat racine" })]);
      drawer.appendChild(cert);
    }
  }

  function renderHome() {
    home.textContent = "";
    home.appendChild(el("h2", { text: "Services" }));
    home.appendChild(el("p", { "class": "muted", text: "Choisis un service : il s'ouvre dans un onglet, sans quitter cette page." }));
    groups().forEach(function (group) {
      home.appendChild(el("h3", { text: group.title }));
      var cards = el("div", { "class": "cards" });
      group.items.forEach(function (s) {
        var card = el("button", { "class": "card", type: "button" }, [
          el("span", { "class": "head" }, [icon(s.icon), el("span", { text: s.title }), dot(s.name)]),
          el("span", { "class": "desc", text: s.description })
        ]);
        card.addEventListener("click", function () { openService(s.name); });
        cards.appendChild(card);
      });
      home.appendChild(cards);
    });
    if (hasCa) {
      var link = el("a", { href: "/ca.crt", text: "Télécharger le certificat racine" });
      home.appendChild(el("div", { "class": "help" }, [
        el("p", { text: "Un service reste blanc ou affiche un avertissement ? Installe le certificat racine de Caddy dans ton navigateur, une seule fois." }),
        link
      ]));
    }
  }

  // --- statut des services (points vert / rouge) ---
  function applyStatus(status) {
    document.querySelectorAll("[data-dot]").forEach(function (node) {
      var up = status[node.getAttribute("data-dot")];
      node.className = "dot" + (up === true ? " up" : up === false ? " down" : "");
    });
  }
  function fetchJson(url) {
    return fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } }).then(function (res) {
      if (res.status === 401) { window.location.href = "/login"; throw new Error("session expirée"); }
      return res.json();
    });
  }
  function refreshStatus() {
    fetchJson("/api/status").then(applyStatus).catch(function () { /* hors ligne : on garde l'affichage */ });
  }

  // --- onglets ---
  function select(name) {
    active = name;
    home.hidden = name !== "home";
    order.forEach(function (n) {
      var selected = n === name;
      opened[n].frame.hidden = !selected;
      opened[n].tab.setAttribute("aria-selected", String(selected));
    });
    homeTab.setAttribute("aria-selected", String(name === "home"));
    document.querySelectorAll(".nav-item[data-nav]").forEach(function (node) {
      node.classList.toggle("active", node.getAttribute("data-nav") === name);
    });
    externalBtn.hidden = name === "home";
    var title = name === "home" ? "expolab" : (byName[name] ? byName[name].title + " · expolab" : "expolab");
    document.title = title;
    var wanted = name === "home" ? "" : "#" + name;
    if (window.location.hash !== wanted && (wanted || window.location.hash)) {
      history.replaceState(null, "", window.location.pathname + wanted);
    }
  }

  function activate(name) { select(name); }

  function openService(name) {
    var svc = byName[name];
    if (!svc) { select("home"); return; }
    if (!svc.framed) { window.open(svc.url, "_blank", "noopener"); return; }
    if (!opened[name]) {
      var close = el("span", { "class": "close", role: "button", "aria-label": "Fermer " + svc.title, tabindex: "0" }, []);
      close.appendChild((function () { var s = icon("x"); return s; })());
      var tab = el("button", { "class": "tab", type: "button", role: "tab", "aria-selected": "false", "data-tab": name }, [dot(name), el("span", { text: svc.title }), close]);
      tab.addEventListener("click", function (ev) {
        if (ev.target.closest(".close")) { closeService(name); } else { activate(name); }
      });
      close.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); closeService(name); } });

      var notice = el("div", { "class": "notice", hidden: "" }, []);
      notice.appendChild(el("span", { text: svc.title + " met du temps à s'afficher, ou refuse d'être affiché ici." }));
      var openAlone = el("button", { type: "button", text: "Ouvrir à part" });
      openAlone.addEventListener("click", function () { window.open(svc.url, "_blank", "noopener"); });
      notice.appendChild(openAlone);

      var iframe = el("iframe", { title: svc.title, allow: "fullscreen; clipboard-read; clipboard-write; camera", referrerpolicy: "same-origin" }, []);
      var timer = setTimeout(function () { notice.hidden = false; }, 12000);
      iframe.addEventListener("load", function () { clearTimeout(timer); notice.hidden = true; });
      iframe.src = svc.url;

      var frame = el("div", { "class": "frame", role: "tabpanel", hidden: "" }, [notice, iframe]);
      stage.appendChild(frame);
      tabsBar.appendChild(tab);
      opened[name] = { tab: tab, frame: frame };
      order.push(name);
      refreshStatus();
    }
    select(name);
  }

  // "Compte" : la section Identifiants de la page caddy-admin, dans un onglet a part.
  function openAccount() {
    if (!byName.caddy) return;
    if (!byName.compte) {
      byName.compte = { name: "compte", title: "Identifiants", group: "Compte", icon: "user", description: "", framed: true, url: byName.caddy.url + "#identifiants" };
    }
    openService("compte");
  }

  function closeService(name) {
    var entry = opened[name];
    if (!entry) return;
    entry.tab.remove();
    entry.frame.remove();
    delete opened[name];
    order = order.filter(function (n) { return n !== name; });
    if (active === name) select(order.length ? order[order.length - 1] : "home");
  }

  var homeTab = el("button", { "class": "tab", type: "button", role: "tab", "aria-selected": "true" }, [icon("home"), el("span", { text: "Accueil" })]);
  homeTab.addEventListener("click", function () { activate("home"); });
  tabsBar.appendChild(homeTab);

  externalBtn.addEventListener("click", function () {
    var svc = byName[active];
    if (svc) window.open(svc.url, "_blank", "noopener");
  });

  function fromHash() {
    var name = window.location.hash.replace(/^#/, "");
    if (name === "compte") openAccount();
    else if (name && byName[name]) openService(name);
    else select("home");
  }
  window.addEventListener("hashchange", fromHash);

  // caddy-admin (dans un cadre) previent quand le couple d'identifiants a change :
  // les sessions sont fermees, retour a la connexion. Seules les pages du meme
  // domaine sont ecoutees.
  window.addEventListener("message", function (ev) {
    var domain = document.body.getAttribute("data-domain");
    var host = "";
    try { host = new URL(ev.origin).hostname; } catch (e) { return; }
    if (host !== domain && !host.endsWith("." + domain)) return;
    if (ev.data && ev.data.type === "expolab-credentials-changed") window.location.href = "/login";
  });

  // --- demarrage ---
  fetchJson("/api/services").then(function (data) {
    services = data.services || [];
    services.forEach(function (s) { byName[s.name] = s; });
    renderDrawer();
    renderHome();
    var status = {};
    services.forEach(function (s) { status[s.name] = s.up; });
    applyStatus(status);
    fromHash();
    setInterval(refreshStatus, 15000);
  });
})();

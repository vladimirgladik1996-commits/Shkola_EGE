/* «ИИ-проверка»: раскладка блоков отчёта по страницам A4 — та же вёрстка, что в макете ai-check-a4-preview.html.
   Python (essayreport.html_report) отдаёт window.REPORT: шапку/подвал и список блоков; здесь блоки по порядку
   ставятся в <section class="page">, и когда страница переполнена — начинается следующая (шапка и подвал
   «Стр. N из M» на каждой). Всё, что помещается в макет, ложится ровно так же, как в макете.
   Блок: h — HTML; w — обёртка (essay | rcs | tbl: соседние блоки с одной обёрткой живут в одной рамке/таблице);
   k — «держать со следующим» (заголовок не остаётся один внизу страницы); n — начать с новой страницы. */
(function () {
  var C = window.REPORT, host = document.getElementById("pages");

  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }

  function mkTop(n) {                                   // .top макета: логотип, бренд + подпись, дата и номер
    var top = el("div", "top");
    if (C.logo) { var im = el("img"); im.src = C.logo; im.alt = ""; top.appendChild(im); }
    var br = el("div", "brand");
    br.appendChild(document.createTextNode(C.brand));
    br.appendChild(el("small", null, n === 1 ? C.sub1 : C.sub2));
    top.appendChild(br);
    var mt = el("div", "meta");
    mt.appendChild(document.createTextNode(C.date));
    mt.appendChild(document.createElement("br"));
    mt.appendChild(document.createTextNode("Отчёт № " + C.num));
    top.appendChild(mt);
    return top;
  }

  function mkWrap(kind) {
    if (kind === "essay") { var d = el("div", "essay"); return { root: d, body: d }; }
    if (kind === "rcs") { var r = el("div", "rcs"); return { root: r, body: r }; }
    var t = document.createElement("table");            // tbl: шапка таблицы повторяется на каждой странице
    t.innerHTML = C.thead + "<tbody></tbody>";
    return { root: t, body: t.querySelector("tbody") };
  }

  function mkBlock(b) {                                 // tr нельзя парсить внутри div — отдельный контейнер
    if (b.w === "tbl") { var tb = document.createElement("tbody"); tb.innerHTML = b.h; return tb.firstElementChild; }
    var d = document.createElement("div"); d.innerHTML = b.h; return d.firstElementChild;
  }

  var pages = [], cur, grow, wrap, wrapKind, count;

  function newPage() {
    cur = el("section", "page");
    cur.style.height = "296mm";                         // в макете min-height:296mm; фиксируем, чтобы переполнение было видно (scrollHeight)
    cur.appendChild(mkTop(pages.length + 1));
    grow = el("div", "grow");
    var foot = el("div", "foot");
    foot.appendChild(el("span", null, C.brand));
    foot.appendChild(el("span", null, ""));
    cur.appendChild(grow);
    cur.appendChild(foot);
    host.appendChild(cur);
    pages.push(cur);
    wrap = null; wrapKind = null; count = 0;
  }

  function over() { return cur.scrollHeight > cur.clientHeight + 1; }

  function put(b, node) {                               // ставит блок на текущую страницу, возвращает запись для отката
    var rec = { node: node, b: b, newWrap: null };
    if (b.w) {
      if (!wrap || wrapKind !== b.w) { wrap = mkWrap(b.w); wrapKind = b.w; rec.newWrap = wrap; cur.insertBefore(wrap.root, grow); }
      wrap.body.appendChild(node);
    } else {
      cur.insertBefore(node, grow); wrap = null; wrapKind = null;
    }
    if (count === 0 && node.tagName === "H2") node.style.marginTop = "2px";   // как у h2 первой строки страниц 2–3 макета
    count++;
    return rec;
  }

  function undo(recs) {
    for (var i = recs.length - 1; i >= 0; i--) {
      var r = recs[i];
      r.node.parentNode.removeChild(r.node);
      if (r.newWrap) { r.newWrap.root.parentNode.removeChild(r.newWrap.root); wrap = null; wrapKind = null; }
      count--;
    }
  }

  // группы: блок с k=1 держится со следующим
  var groups = [], g = [];
  C.blocks.forEach(function (b) {
    g.push(b);
    if (!b.k) { groups.push(g); g = []; }
  });
  if (g.length) groups.push(g);

  function run() {
    newPage();
    groups.forEach(function (grp) {
      if (grp[0].n && count > 0) newPage();
      var nodes = grp.map(mkBlock), recs = [];
      for (var i = 0; i < grp.length; i++) recs.push(put(grp[i], nodes[i]));
      if (over() && count > recs.length) {              // не влезло, а на странице уже что-то есть — на новую
        undo(recs);
        newPage();
        recs = [];
        for (var j = 0; j < grp.length; j++) recs.push(put(grp[j], nodes[j]));
      }
    });
    var n = pages.length;
    pages.forEach(function (p, i) { p.querySelector(".foot span:last-child").textContent = "Стр. " + (i + 1) + " из " + n; });
    window.__pages = pages.map(function (p) { return p.innerText; });
  }

  var started = false;
  function go() { if (started) return; started = true; run(); }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(go); else go();
  setTimeout(go, 8000);                                 // шрифты не пришли — раскладываем тем, что есть
})();

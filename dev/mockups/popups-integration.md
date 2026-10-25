# Поп-апы «не хватает фото» в «ИИ-проверке»

Макет: `essay-check-popups.html` (светлая/тёмная тема, ЕГЭ/ОГЭ, кнопки рабочие).
Показываются после «Готово», если загружены не оба набора фото.

| Что снято | Поп-ап | Кнопки | Действие |
|---|---|---|---|
| Работа + задание | нет | нет | сразу `ecUpload` |
| Только работа | «Не вижу задания» | «Добавить задание» · «Продолжить без задания» | добавить → `ecSetCh("t")`; продолжить → загрузка с `ecUpload(true)`, в API `no_task:true` |
| Только задание | «Не вижу сочинения» / «Не вижу эссе» | «Добавить» | `ecSetCh("w")`, пропустить нельзя |
| Ничего | прежний `toast("Сначала сделайте хотя бы одно фото")` | | |

Тексты и цвета — из макета; золотая точка в строке статуса — можно продолжить, красная — нельзя.
Подложка `.ec-mod` и анимации `ecFade`/`ecPop2` уже есть в `docs/index.html`, свои — только `.eck*`.

## 1. CSS (после блока `.ec-mod` в `docs/index.html`)

В переменную `--eck-av` положить webp аватара Вишенки (в макете — `essay-check-popups.html`, класс `.eck-av`;
исходник `assets/vishenka-avatar.jpg`, кадр 176×176) — либо `:root{--eck-av:url(data:image/webp;base64,…)}`.

```css
.eck{position:relative;width:100%;max-width:340px;background:var(--bg);color:var(--tx);border-radius:24px;padding:50px 16px 16px;box-shadow:0 14px 40px rgba(0,0,0,.4);animation:ecPop2 .34s cubic-bezier(.2,1.15,.4,1) both;text-align:center}
.eck-av{position:absolute;left:50%;top:-40px;width:80px;height:80px;margin-left:-40px;border-radius:50%;background:var(--eck-av) center/cover;border:4px solid var(--yel);box-shadow:0 0 0 4px var(--bg),0 8px 18px var(--sh)}
.eck h3{font-size:20px;font-weight:900;line-height:1.2}
.eck>p{margin:8px 4px 12px;font-size:13.5px;font-weight:700;line-height:1.45;color:var(--hint)}
.eck>p b{color:var(--tx);font-weight:800}
.eck-st{background:var(--card);border:1.5px solid var(--ln);border-radius:14px;padding:4px 12px;margin-bottom:14px;text-align:left}
.eck-sr{display:flex;align-items:center;gap:10px;padding:8px 0;font-size:13.5px;font-weight:800}
.eck-sr+.eck-sr{border-top:1.5px dashed var(--ln)}
.eck-sr .d{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font:900 14px/1 Nunito,system-ui,sans-serif;color:#fff;background:var(--grn)}
.eck-sr .v{margin-left:auto;font-weight:800;color:var(--hint)}
.eck-sr.no .d{background:var(--yel);color:#0E2250}
.eck-sr.no.req .d{background:var(--red);color:#fff}
.eck-sr.no .v{color:var(--tx)}
.eck-bf,.eck-bg{width:100%;border-radius:16px;padding:14px;font:900 15px Nunito,system-ui,sans-serif;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px}
.eck-bf{border:1.5px solid var(--acc);background:var(--acc);color:var(--on)}
.eck-bf svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.eck-bg{margin-top:8px;border:1.5px solid var(--ln);background:none;color:var(--hint)}
.eck-bf:active,.eck-bg:active{transform:scale(.98)}
@media (prefers-reduced-motion:reduce){.ec-mod,.eck,.chip.hit{animation:none}}
```

## 2. JS (рядом с `ecUpload`)

```js
/* поп-ап «не хватает фото»: "nt" — есть работа, нет задания; "nw" — есть задание, нет работы */
function ecAskUi(){let o=$("#ec-ask");if(o)return o;
  o=document.createElement("div");o.id="ec-ask";o.className="ec-mod";o.hidden=true;
  o.innerHTML=`<div class="eck" role="dialog" aria-modal="true" aria-labelledby="eck-t"><div class="eck-av" aria-hidden="true"></div>
  <h3 id="eck-t"></h3><p id="eck-p"></p><div class="eck-st" id="eck-s"></div>
  <button type="button" class="eck-bf" id="eck-add"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg><span></span></button>
  <button type="button" class="eck-bg" id="eck-skip">Продолжить без задания</button></div>`;
  document.body.appendChild(o);return o}
function ecAskOpen(why){   /* фон не закрывает: решение нужно принять; Esc — закрыть и остаться на съёмке */
  const o=ecAskUi(),nt=why==="nt",w=ecShots.w.length,t=ecShots.t.length,ess=ecKind==="oge"?"эссе":"сочинения";
  $("#eck-t").textContent=nt?"Не вижу задания":"Не вижу "+ess;
  $("#eck-p").innerHTML=nt?"Без опорного текста и условия я оценю только <b>грамотность, речь и композицию</b>. Критерии про смысл текста пропущу — угадывать не буду."
    :"Задание есть, а рукописного "+ess+" нет — проверять нечего. <b>Сфотографируйте работу</b>.";
  const row=(n,c,req)=>`<div class="eck-sr${c?"":" no"+(req?" req":"")}"><span class="d">${c?"✓":"!"}</span>${n}<span class="v">${c?c+" фото":"нет фото"}</span></div>`;
  $("#eck-s").innerHTML=row("Работа",w,true)+row("Задание",t,false);
  $("#eck-add span").textContent=nt?"Добавить задание":"Добавить";
  $("#eck-skip").hidden=!nt;
  $("#eck-add").onclick=()=>{ecAskClose();ecSetCh(nt?"t":"w")};
  $("#eck-skip").onclick=()=>{ecAskClose();ecUpload(true)};
  o.hidden=false;$("#eck-add").focus()}
function ecAskClose(){const o=$("#ec-ask");if(o&&!o.hidden)o.hidden=true}
document.addEventListener("keydown",e=>{if(e.key==="Escape")ecAskClose()});
```

## 3. Хук в `ecUpload`

```diff
-async function ecUpload(){if(ecBusy)return;
+async function ecUpload(skip){if(ecBusy)return;
   if(!ecCnt()){toast("Сначала сделайте хотя бы одно фото");return}
+  if(skip!==true&&!(ecShots.w.length&&ecShots.t.length)){ecAskOpen(ecShots.w.length?"nt":"nw");return}   /* поп-апы: не хватает фото */
```

`onclick="ecUpload()"` вызывается без аргументов, поэтому поп-ап срабатывает; `ecUpload(true)` его обходит.

## 4. Бэкенд (на будущее, сейчас API не подключён)

- «Продолжить без задания»: в `/api/essay_check/upload` `t` пустой; в `/run` передавать `no_task:true`.
- Кима: не оценивать критерии, зависящие от опорного текста и условия, и не выдумывать их; в итоговой оценке указать, что они пропущены.
- Только задание без работы до сервера не доходит — блокируется на клиенте.

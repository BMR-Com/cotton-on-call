/* Weekly On-Call Brief — renders data/intel/brief.json (published weekly by BMR-Com). */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var k = function (n) { if (n == null) return '–'; var a = Math.abs(n); return (n < 0 ? '−' : '') + (a >= 1000 ? (a / 1000).toFixed(1) + 'k' : Math.round(a)); };
  var sg = function (n) { return n == null ? '–' : (n > 0 ? '+' : n < 0 ? '−' : '') + Math.abs(n).toLocaleString(); };
  var pc = function (x) { return x == null ? '–' : (x >= 0 ? '+' : '−') + Math.abs(x * 100).toFixed(1) + '%'; };
  var md = function (t) { return String(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>'); };
  function kpi(l, v, s, t) { return '<div class="ik ' + (t || '') + '"><span>' + l + '</span><b>' + v + '</b><small>' + (s || '') + '</small></div>'; }
  function render(B) {
    var L = B.latest, S = B.spread;
    $('intelSub').innerHTML = 'On-call as of <b>' + L.oncall_date + '</b> · COT as of <b>' + L.cot_date + '</b> · ' + L.my + ' marketing year · updated ' + B.generated;
    $('intelKpis').innerHTML =
      kpi('Old-crop sales (mills)', k(L.S_old), sg(L.dS_old) + ' w/w · ' + Math.round(L.S_old_seas_pct) + 'th pct vs 5 seasons') +
      kpi('Old-crop purchases', k(L.P_old), sg(L.dP_old) + ' w/w · ' + Math.round(L.P_old_seas_pct) + 'th pct') +
      kpi('Total purchases', k(L.P), sg(L.dP8) + ' over 8w') +
      kpi('Old-crop sales ÷ PMU long', L.Sold_pmuL.toFixed(2) + '×', L.Sold_pmuL > 2 ? 'mills uncovered' : L.Sold_pmuL < 1 ? 'mills well covered' : 'mixed cover') +
      kpi('Front: ' + L.front, k(L.S_front) + ' / ' + k(L.P_front), 'sales / purchases · ' + L.wks_to_fnd + ' wks to FND') +
      kpi('Front/next spread', S ? (S.now >= 0 ? '+' : '') + S.now.toFixed(2) + '¢' : '–', S && S.wk_ago != null ? ((S.now - S.wk_ago) >= 0 ? '+' : '') + (S.now - S.wk_ago).toFixed(2) + '¢ w/w' : '') +
      kpi('Managed money', k(L.mm_net), sg(L.mm_chg) + ' w/w · COT idx ' + Math.round(L.mm_idx)) +
      (L.px != null ? kpi('ICE cotton (front)', L.px.toFixed(2), L.front) : '');
    $('intelBrief').innerHTML = B.brief.map(function (b) { return '<div class="ibh">' + b.h + '</div><div>' + md(b.t) + '</div>'; }).join('');
  }
  fetch('./data/intel/brief.json?t=' + Date.now()).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(render).catch(function () { var s = $('intelSec'); if (s) s.style.display = 'none'; });
})();

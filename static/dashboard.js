/*
 * Wattson dashboard scripts.
 *
 * Loaded as /dashboard.js by the served dashboard. Demo builds
 * (WATTSON_DEMO set) inline this file into the single demo page
 * instead; report.py handles both from this one source.
 */

/* Main dashboard behavior. */
(function() {
  var el = document.getElementById('poll-age');
  if (!el || !el.dataset.ts) return;
  var timestamp = Number(el.dataset.ts);
  if (!Number.isFinite(timestamp)) return;
  function updatePollAge() {
    var seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
    var age = 'just now';
    if (seconds >= 86400) {
      var days = Math.floor(seconds / 86400);
      age = days + (days === 1 ? ' day ago' : ' days ago');
    } else if (seconds >= 3600) {
      age = Math.floor(seconds / 3600) + ' hr ago';
    } else if (seconds >= 60) {
      age = Math.floor(seconds / 60) + ' min ago';
    }
    el.textContent = 'Last poll ' + age;
  }
  updatePollAge();
  setInterval(updatePollAge, 60000);
})();
/* Evaluated at use time, not cached, so an OS-level change while the page is
   open takes effect on the next interaction. */
function prefersReducedMotion() {
  return !!(window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}
/* Segmented range controls (battery 7/14/30d, pack health and overnight
   trend 1M/6M/1Y/All): one helper, each button shows its own panel. */
function segToggle(segId, prefix) {
  var seg = document.getElementById(segId);
  if (!seg) return;
  seg.querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => {
      seg.querySelectorAll('button')
        .forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      seg.querySelectorAll('button').forEach(x => {
        document.getElementById(prefix + x.dataset.n).hidden = (x !== b);
      });
    });
  });
}
segToggle('battseg', 'batt');
segToggle('packseg', 'pack');
segToggle('trendseg', 'trend');
/* Daily activity: progressive list. Day groups ship as JSON; the latest 7 render
   first, "Show earlier" appends 7 more, and the month dropdown jumps to a
   month (showing that whole month, then earlier from there). */
(function () {
  var dataEl = document.getElementById('actdata');
  var listEl = document.getElementById('actdays');
  var moreBtn = document.getElementById('actshowmore');
  var monthSel = document.getElementById('actmonth');
  if (!dataEl || !listEl || !moreBtn) return;
  var days = JSON.parse(dataEl.textContent).days;
  var PAGE = 7, start = 0, shown = PAGE;
  /* render() rebuilds the whole list; render(from) appends days[from..end)
     to what is already on screen, so rows the user has expanded stay open
     and the scroll position holds when "Show earlier" is pressed. */
  function render(from) {
    var end = Math.min(start + shown, days.length), html = '', i, d;
    if (from == null) { listEl.innerHTML = ''; from = start; }
    for (i = from; i < end; i++) {
      d = days[i];
      html += '<details class="aday"><summary><div class="asum"><span>' +
        d.title + '</span><span class="dim">' + d.summary + '</span></div>' +
        d.abar + '</summary>' + d.body + '</details>';
    }
    listEl.insertAdjacentHTML('beforeend', html);
    moreBtn.hidden = end >= days.length;
  }
  moreBtn.addEventListener('click', function () {
    var from = Math.min(start + shown, days.length);
    var prevCount = from - start;
    shown += PAGE;
    render(from);
    if (prefersReducedMotion()) return;
    /* Staggered entrance for the freshly appended rows. */
    var rows = listEl.querySelectorAll('details.aday');
    var fresh = rows.length - prevCount, i;
    for (i = 0; i < fresh; i++) {
      rows[prevCount + i].animate(
        [{opacity: 0, transform: 'translateY(14px)'},
         {opacity: 1, transform: 'translateY(0)'}],
        {duration: 340, delay: i * 60, easing: 'cubic-bezier(0.4,0,0.2,1)',
         fill: 'backwards'});
    }
  });
  if (monthSel) monthSel.addEventListener('change', function () {
    var m = monthSel.value, i;
    start = 0; shown = PAGE;
    if (m !== 'latest') {
      for (i = 0; i < days.length; i++)
        if (days[i].month === m) { start = i; break; }
      shown = 0;
      for (i = start; i < days.length && days[i].month === m; i++) shown++;
    }
    render();
  });
  render();
})();

/* Smooth accordions: intercept the native <details> toggle on day rows,
   charging rows, and the night-by-night breakdown, and animate the body
   height instead. Own IIFE (not inside the activity one, which returns early
   when there is no activity data). State is read from the live `open`
   attribute, so toggles done by the browser itself (find-in-page auto-open,
   #fragment navigation) can never desync it; data-closing marks only the
   in-flight collapse, during which `open` is still set. */
(function () {
  var EASE = 'cubic-bezier(0.4,0,0.2,1)';
  function isOpen(details) {
    return details.open && details.dataset.closing !== '1';
  }
  function setAccOpen(details, open) {
    var body = details.querySelector('.abody, .chargedetail');
    if (!body) { details.open = open; return; }
    /* Measure BEFORE cancelling: cancel() snaps the body back to its natural
       size, so reversing a half-finished animation would otherwise jump. */
    var from = details.open ? body.getBoundingClientRect().height : 0;
    var op = details.open ? parseFloat(getComputedStyle(body).opacity) : .3;
    body.getAnimations().forEach(function (a) { a.cancel(); });
    if (open) {
      delete details.dataset.closing;
      details.open = true;
      /* Lazy images reserve their box via width/height attributes, so
         scrollHeight is already final here. */
      var to = body.scrollHeight;
      body.animate([{height: from + 'px', opacity: op},
                    {height: to + 'px', opacity: 1}],
                   {duration: 320, easing: EASE});
    } else {
      details.dataset.closing = '1';
      var anim = body.animate([{height: from + 'px', opacity: op},
                               {height: '0px', opacity: .3}],
                              {duration: 260, easing: EASE, fill: 'forwards'});
      anim.onfinish = function () {
        /* fill:forwards holds the collapsed state until `open` is removed,
           so there is no frame at full height. Cancel afterwards so no stale
           fill can hide the body if the page later toggles natively. */
        details.open = false;
        delete details.dataset.closing;
        anim.cancel();
      };
    }
  }
  document.addEventListener('click', function (e) {
    var t = e.target;
    var summary = (t && t.closest) ? t.closest('summary') : null;
    if (!summary) return;
    var details = summary.parentElement;
    if (!details || details.tagName !== 'DETAILS') return;
    if (!details.classList.contains('aday') &&
        !details.classList.contains('sacc')) return;
    if (prefersReducedMotion() ||
        !details.querySelector('.abody, .chargedetail')) return;
    e.preventDefault();
    setAccOpen(details, !isOpen(details));
  });
})();

/* Back-to-top: appears past 600px of scroll. rAF-throttled scroll
   check: explicit and unambiguous beats clever. */
(function () {
  var btn = document.getElementById('toTop');
  if (!btn) return;
  var ticking = false;
  function update() {
    btn.classList.toggle('show',
      window.scrollY > 600);
    ticking = false;
  }
  window.addEventListener('scroll', function () {
    if (!ticking) { ticking = true; requestAnimationFrame(update); }
  }, {passive: true});
  update();
  btn.addEventListener('click', function () {
    window.scrollTo({top: 0,
                     behavior: prefersReducedMotion() ? 'auto' : 'smooth'});
    /* The button hides itself once we are near the top, which would drop
       keyboard focus to <body>. Park focus on the page container instead so
       the next Tab continues from the top of the page. */
    var pageTop = document.querySelector('.wrap');
    if (pageTop) {
      pageTop.setAttribute('tabindex', '-1');
      pageTop.focus({preventScroll: true});
    }
  });
})();

/* Trip maps are static, tile-backed images rendered at build time (the
   viewer blocks external network), so no client-side map library is needed. */

;

/* Smooth scrolling for in-page anchor links. */
var scrollRaf=null;(function(){var ease=function(t){return t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2};document.querySelectorAll('a[href^="#"]').forEach(function(a){a.addEventListener('click',function(e){var el=document.querySelector(a.getAttribute('href'));if(!el)return;e.preventDefault();var y=el.getBoundingClientRect().top+window.scrollY-document.querySelector('.sitenav').getBoundingClientRect().bottom-12;cancelAnimationFrame(scrollRaf);var s=window.scrollY,d=y-s,dur=900,t0=performance.now();(function step(t){var p=Math.min((t-t0)/dur,1);window.scrollTo(0,s+d*ease(p));if(p<1)scrollRaf=requestAnimationFrame(step);})(t0);});});})();

;

/* Active-section highlighting in the top nav. */
(function(){var links={};document.querySelectorAll('.nav-links a[href^="#"]').forEach(function(a){links[a.getAttribute('href').slice(1)]=a});var ids=Object.keys(links);var secs=ids.map(function(id){return document.getElementById(id)}).filter(Boolean).sort(function(a,b){return a.getBoundingClientRect().top-b.getBoundingClientRect().top});if(!secs.length)return;var cur='',locked=null,lockTimer=null;function setActive(id){if(id===cur)return;cur=id;ids.forEach(function(k){links[k].classList.toggle('active',k===id)})}function update(){if(locked){setActive(locked);return}var line=120,id='';secs.forEach(function(s){if(s.getBoundingClientRect().top<=line){id=s.id}});var lastSec=secs[secs.length-1];var distToBottom=document.documentElement.scrollHeight-(window.innerHeight+window.scrollY);if(lastSec&&distToBottom<=300&&lastSec.getBoundingClientRect().top<window.innerHeight){id=lastSec.id}setActive(id)}document.querySelectorAll('.nav-links a[href^="#"]').forEach(function(a){a.addEventListener('click',function(){locked=a.getAttribute('href').slice(1);setActive(locked);clearTimeout(lockTimer);lockTimer=setTimeout(function(){locked=null;update()},1000)})});var ticking=false;window.addEventListener('scroll',function(){if(ticking)return;ticking=true;requestAnimationFrame(function(){ticking=false;update()})},{passive:true});update();})();

;

/* DC fast-charging cost entry modal. */
var _costChargeId=null;var _costStartTs=null;function openCostModal(chargeId,currentVal,startTs){_costChargeId=chargeId;_costStartTs=(startTs===undefined||startTs===null)?'':startTs;document.getElementById('costinput').value=((currentVal===null||currentVal===undefined)?'':currentVal);document.getElementById('costmodal').style.display='flex';document.getElementById('costinput').focus();}function closeCostModal(){document.getElementById('costmodal').style.display='none';_costChargeId=null;}function saveCost(){var v=parseFloat(document.getElementById('costinput').value);if(v===null||v===undefined||isNaN(v)||v<0){alert('Enter a valid price');return;}document.getElementById('form_charge_id').value=_costChargeId;document.getElementById('form_start_ts').value=_costStartTs;document.getElementById('form_cost_per_kwh').value=v;document.getElementById('costform').submit();}function clearCost(){if(!confirm('Clear the entered cost for this session?'))return;document.getElementById('form_charge_id').value=_costChargeId;document.getElementById('form_start_ts').value=_costStartTs;document.getElementById('form_cost_per_kwh').value='';document.getElementById('costform').submit();}document.getElementById('costmodal').addEventListener('click',function(e){if(e.target===this)closeCostModal();});document.addEventListener('keydown',function(e){if(e.key==='Escape'){var m=document.getElementById('costmodal');if(m&&m.style.display!=='none')closeCostModal();}});

;

/* Send destination modal. */
var _navCar = null, _navWhMi = 380, _navSaved = [], _navListArr = [],
    _navPlace = null, _navSelIdx = null, _navMode = 'direct', _navRound = false,
    _navJob = null, _navTimer = null, _navSearchTimer = null, _navTab = 'rec';
var _navUsableKwh = 87.9;  // refreshed from /api/nav/state (config.USABLE_KWH)
var NAV_ROAD_FACTOR = 1.3;  // straight-line distance to road estimate
function openNavModal() {
  _navPlace = null; _navSelIdx = null; _navMode = 'direct'; _navRound = false; _navJob = null;
  _navTab = 'rec';
  document.getElementById('navmodal').style.display = 'flex';
  document.getElementById('navq').value = '';
  navUpdateClear();
  navParkSel();
  document.getElementById('navstatus').textContent = '';
  document.getElementById('navlist').innerHTML = '<p class="sub">Loading your places...</p>';
  navSetSend(false, 'Send');
  fetch('/api/nav/state').then(function(r) { return r.json(); }).then(function(j) {
    if (j && j.ok === false && j.error) {
      document.getElementById('navlist').innerHTML = '<p class="sub">' + navEsc(j.error) + '</p>';
      return;
    }
    _navCar = j.car || null; _navWhMi = j.wh_mi || 380;
    if (j.usable_kwh) _navUsableKwh = j.usable_kwh;
    _navSaved = j.places || [];
    navShowBrowse();
  }).catch(function() {
    document.getElementById('navlist').innerHTML = '<p class="sub">Could not load your places. Search still works.</p>';
  });
  document.getElementById('navq').focus();
}
function closeNavModal() {
  document.getElementById('navmodal').style.display = 'none';
  if (_navTimer) { clearInterval(_navTimer); _navTimer = null; }
}
function navSetSend(enabled, label) {
  var b = document.getElementById('navsendbtn');
  b.disabled = !enabled; b.style.opacity = enabled ? '1' : '.45';
  if (label) b.textContent = label;
}
function navSegBtn(el, active) {
  if (!el) return;
  el.style.background = active ? '#f2f2f2' : '#24262a';
  el.style.color = active ? '#111' : '#9aa0a6';
  el.style.borderColor = active ? '#f2f2f2' : '#33373d';
  el.style.fontWeight = active ? '600' : '500';
}
function navUpdateClear() {
  var b = document.getElementById('navclear');
  var q = document.getElementById('navq');
  if (b && q) b.style.display = q.value ? 'block' : 'none';
}
function navClearSearch() {
  var q = document.getElementById('navq');
  if (_navSearchTimer) { clearTimeout(_navSearchTimer); _navSearchTimer = null; }
  if (q) q.value = '';
  navUpdateClear();
  navShowBrowse();
  if (q) q.focus();
}
function navParkSel() {
  var sel = document.getElementById('navsel');
  var home = document.getElementById('navselhome');
  if (sel && home && sel.parentNode !== home) home.appendChild(sel);
  if (sel) sel.style.display = 'none';
  navClearRowSel();
}
function navClearRowSel() {
  var rows = document.querySelectorAll('#navlist .navrow');
  for (var r = 0; r < rows.length; r++) {
    rows[r].style.borderColor = '#2c2f34';
    rows[r].style.background = 'transparent';
  }
}
function navExpandSel(row) {
  var sel = document.getElementById('navsel');
  if (!sel) return;
  if (row) row.appendChild(sel);
  sel.style.display = 'block';
  navClearRowSel();
  if (row) {
    row.style.borderColor = '#7ed321';
    row.style.background = 'rgba(126,211,33,.07)';
    setTimeout(function() { row.scrollIntoView({block: 'start', behavior: 'smooth'}); }, 40);
  }
}
function navRow(p, i) {
  var dist = (p.distance_mi !== undefined && p.distance_mi !== null) ? ' &middot; ' + p.distance_mi + ' mi' : '';
  return '<div class="navrow" onclick="navPick(' + i + ', this)" style="padding:9px 10px;border:1px solid #2c2f34;border-radius:10px;margin-bottom:8px;cursor:pointer;background:transparent">' +
    '<div style="font-weight:600">' + navEsc(p.name) + '</div>' +
    '<div style="color:#a1a1a6;font-size:13px;margin-top:4px;line-height:1.4">' + navEsc(p.address || '') + dist + '</div></div>';
}
function navEsc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function navSetTab(t) {
  _navTab = t;
  navShowBrowse();
}
function navShowBrowse() {
  navParkSel(); _navPlace = null; _navSelIdx = null; navSetSend(false, 'Send');
  document.getElementById('navtabs').style.display = 'flex';
  navSegBtn(document.getElementById('navtabrec'), _navTab === 'rec');
  navSegBtn(document.getElementById('navtabfav'), _navTab === 'fav');
  var seen = {}, out = [];
  _navSaved.forEach(function(p) {
    var isFav = (p.type || '').indexOf('FAVORITE') === 0;
    if ((_navTab === 'fav') !== isFav) return;
    var key = p.place_id || (p.lat + ',' + p.lon);
    if (seen[key]) return; seen[key] = 1; out.push(p);
  });
  _navListArr = (_navTab === 'rec') ? out.slice(0, 12) : out;
  var html = '';
  _navListArr.forEach(function(p, i) { html += navRow(p, i); });
  document.getElementById('navlist').innerHTML = html ||
    ('<p class="sub">' + (_navTab === 'fav' ? 'No favorites saved in Rivian.' : 'No recent places.') + '</p>');
}
document.getElementById('navq').addEventListener('input', function() {
  navUpdateClear();
  var q = this.value.trim();
  if (_navSearchTimer) clearTimeout(_navSearchTimer);
  if (!q) { navShowBrowse(); return; }
  document.getElementById('navtabs').style.display = 'none';
  if (q.indexOf('http') === 0 && (q.indexOf('maps') >= 0 || q.indexOf('telenav') >= 0 || q.indexOf('goo.gl') >= 0)) {
    _navListArr = [{name: 'Map link', address: q, url: q, lat: null, lon: null, place_id: null}];
    navParkSel(); _navPlace = null; _navSelIdx = null; navSetSend(false, 'Send');
    document.getElementById('navlist').innerHTML = '<p class="sub" style="margin:0 0 8px">Link</p>' + navRow(_navListArr[0], 0);
    return;
  }
  if (q.length < 3) return;
  _navSearchTimer = setTimeout(function() {
    navParkSel(); _navPlace = null; _navSelIdx = null; navSetSend(false, 'Send');
    fetch('/api/nav/search?q=' + encodeURIComponent(q)).then(function(r) { return r.json(); }).then(function(j) {
      if (!j.ok) { document.getElementById('navlist').innerHTML = '<p class="sub">' + navEsc(j.error || 'Search failed') + '</p>'; return; }
      _navListArr = j.results || [];
      var html = _navListArr.length ? '<p class="sub" style="margin:0 0 8px">Results</p>' : '<p class="sub">No matches. Try a street address.</p>';
      _navListArr.forEach(function(p, i) { html += navRow(p, i); });
      document.getElementById('navlist').innerHTML = html;
    }).catch(function() {
      document.getElementById('navlist').innerHTML = '<p class="sub">Search failed. Try again.</p>';
    });
  }, 800);
});
function navPick(i, row) {
  _navPlace = _navListArr[i]; _navSelIdx = i; _navMode = 'direct'; _navRound = false;
  document.getElementById('navplan').innerHTML = '';
  document.getElementById('navstatus').textContent = '';
  var img = document.getElementById('navmap');
  if (_navPlace.lat !== null && _navPlace.lat !== undefined) {
    img.src = '/api/nav/map?lat=' + _navPlace.lat + '&lon=' + _navPlace.lon;
    img.style.display = 'block';
  } else { img.style.display = 'none'; }
  navExpandSel(row);
  navSetSend(true, 'Send');
  navRenderEnergy();
}
function _havMi(lat1, lon1, lat2, lon2) {
  var R = 3958.8, t = Math.PI / 180;
  var x = Math.sin((lat2 - lat1) * t / 2), y = Math.sin((lon2 - lon1) * t / 2);
  var h = x * x + Math.cos(lat1 * t) * Math.cos(lat2 * t) * y * y;
  return 2 * R * Math.asin(Math.sqrt(h));
}
function navEstimate() {
  if (!_navCar || !_navPlace || _navPlace.lat === null || _navPlace.lat === undefined) return null;
  if (_navCar.lat === null || _navCar.soc === null || _navCar.soc === undefined) return null;
  var roadMi = _havMi(_navCar.lat, _navCar.lon, _navPlace.lat, _navPlace.lon) * NAV_ROAD_FACTOR;
  var kwh = roadMi * _navWhMi / 1000;
  var arrival = _navCar.soc - kwh / _navUsableKwh * 100;
  return {roadMi: roadMi, arrival: arrival, rtArrival: _navCar.soc - 2 * kwh / _navUsableKwh * 100};
}
function navRenderEnergy() {
  var line = document.getElementById('navenergy');
  var tight = document.getElementById('navtight');
  var est = navEstimate();
  if (!_navPlace || _navPlace.lat === null || _navPlace.lat === undefined) {
    line.textContent = 'The car will resolve this link. No charge estimate for links.';
    tight.style.display = 'none'; return;
  }
  if (!est) { line.textContent = ''; tight.style.display = 'none'; return; }
  var shown = _navRound ? est.rtArrival : est.arrival;
  line.textContent = 'About ' + Math.round(est.roadMi) + ' mi by road' +
    (_navRound ? ' each way' : '') + ". You'd arrive at about " +
    Math.max(0, Math.round(shown)) + '% at your usual efficiency.';
  var ow = document.getElementById('navow'), rt = document.getElementById('navrt');
  navSegBtn(ow, !_navRound);
  navSegBtn(rt, _navRound);
  if (est.arrival < 45 || est.rtArrival < 45) {
    tight.style.display = 'block';
    var msg = document.getElementById('navtightmsg');
    var btn = document.getElementById('navplanbtn');
    if (shown < 20) {
      msg.textContent = "That's a lower arrival charge than is comfortable.";
      btn.style.display = 'block';
    } else { msg.textContent = ''; btn.style.display = 'none'; }
  } else { tight.style.display = 'none'; }
}
function navSetRound(b) {
  _navRound = b; _navMode = 'direct';
  document.getElementById('navplan').innerHTML = '';
  navSetSend(true, 'Send');
  navRenderEnergy();
}
function navPlan() {
  var btn = document.getElementById('navplanbtn');
  btn.disabled = true; btn.textContent = 'Asking the car...';
  fetch('/api/nav/plan', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place: _navPlace, round_trip: _navRound})
  }).then(function(r) { return r.json(); }).then(function(j) {
    btn.disabled = false; btn.textContent = 'Let the car plan charging';
    if (!j.ok) { document.getElementById('navplan').textContent = j.error || 'Planning failed.'; return; }
    var txt = "The car's plan: " + Math.round(j.distance_mi) + ' mi, about ' +
      j.drive_minutes + ' min of driving, arriving at about ' +
      Math.round(j.final_arrival_soc !== null ? j.final_arrival_soc : j.arrival_soc) + '%.';
    if (j.chargers && j.chargers.length) {
      txt += ' Charging stops: ' + j.chargers.map(function(c) { return c.name + ' (~' + c.minutes + ' min)'; }).join(', ') + '.';
    } else { txt += ' No charging stop needed.'; }
    if (!j.reachable) txt += ' Even with charging, the car says this is not reachable right now.';
    document.getElementById('navplan').textContent = txt;
    _navMode = 'trip';
    navSetSend(true, 'Send trip');
  }).catch(function() {
    btn.disabled = false; btn.textContent = 'Let the car plan charging';
    document.getElementById('navplan').textContent = 'Planning failed. Try again.';
  });
}
function navSend() {
  navSetSend(false);
  document.getElementById('navstatus').textContent = 'Sending to the car...';
  fetch('/api/nav/send', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place: _navPlace, mode: _navMode, round_trip: _navRound})
  }).then(function(r) { return r.json(); }).then(function(j) {
    if (!j.ok) {
      document.getElementById('navstatus').textContent = j.error || 'Send failed.';
      navSetSend(true, _navMode === 'trip' ? 'Send trip' : 'Send'); return;
    }
    _navJob = j.job_id;
    var waited = 0;
    if (_navTimer) clearInterval(_navTimer);
    _navTimer = setInterval(function() {
      waited += 3;
      fetch('/api/nav/status?job=' + _navJob).then(function(r) { return r.json(); }).then(function(s) {
        if (!s.ok) return;
        var job = s.job;
        if (job.state === 'confirmed') {
          clearInterval(_navTimer); _navTimer = null;
          var names = (job.route.destinations || []).map(function(d) { return d.name; }).join(', ');
          var txt = 'Car is navigating to ' + names + '.';
          if (job.route.chargers && job.route.chargers.length) {
            txt += ' Charging stops the car planned: ' + job.route.chargers.map(function(c) { return c.name + ' (~' + c.minutes + ' min)'; }).join(', ') + '.';
          }
          document.getElementById('navstatus').textContent = txt;
        } else if (job.state === 'failed') {
          clearInterval(_navTimer); _navTimer = null;
          document.getElementById('navstatus').textContent = job.error || 'The send failed.';
          navSetSend(true, _navMode === 'trip' ? 'Send trip' : 'Send');
        } else {
          document.getElementById('navstatus').textContent = 'Sent. Waiting for the car to confirm, usually under a minute.';
        }
        if (waited > 170 && _navTimer) {
          clearInterval(_navTimer); _navTimer = null;
          document.getElementById('navstatus').textContent = 'Still no confirmation from the car. Check the car screen before sending again.';
        }
      }).catch(function() {});
    }, 3000);
  }).catch(function() {
    document.getElementById('navstatus').textContent = 'Send failed. Try again.';
    navSetSend(true, _navMode === 'trip' ? 'Send trip' : 'Send');
  });
}
document.getElementById('navmodal').addEventListener('click', function(e) {
  if (e.target === this) closeNavModal();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var m = document.getElementById('navmodal');
    if (m && m.style.display !== 'none') closeNavModal();
  }
});

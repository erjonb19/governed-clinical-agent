/* The wake-up page (backlog item 9, owner's decision D2a). The service runs on
   Render's free plan, which sleeps when idle; a sleeping service cannot show
   its own "please wait" note, so this static page on GitHub Pages does.
   It pings /v1/health with a no-cors fetch (no CORS change needed on the API:
   the response is opaque, but it resolves once anything answers) and then
   sends the visitor to /ui. It holds no key and sends none. */
(function () {
  "use strict";
  var SERVICE = "https://governed-clinical-agent.onrender.com";
  var GIVE_UP_MS = 180000, RETRY_MS = 3000, TRY_TIMEOUT_MS = 20000;
  var started = Date.now();
  var secs = document.getElementById("secs");
  var status = document.getElementById("status");
  var tick = setInterval(function () {
    secs.textContent = String(Math.round((Date.now() - started) / 1000));
  }, 1000);

  function go() {
    clearInterval(tick);
    status.textContent = "The service is awake. Opening it now…";
    window.location.replace(SERVICE + "/ui");
  }

  function ping() {
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, TRY_TIMEOUT_MS);
    fetch(SERVICE + "/v1/health", { mode: "no-cors", cache: "no-store", signal: ctl.signal })
      .then(function () { clearTimeout(timer); go(); })
      .catch(function () {
        clearTimeout(timer);
        if (Date.now() - started > GIVE_UP_MS) {
          clearInterval(tick);
          status.textContent = "The service did not answer within three minutes. " +
            "It may be down; the link below still tries it directly.";
          return;
        }
        setTimeout(ping, RETRY_MS);
      });
  }
  ping();
}());

const KEY = "geminiTerminalPair";
let sending = false;

function validPair(pair) {
  return pair && Number.isInteger(pair.port) && pair.port > 0 && pair.port < 65536 &&
    /^[a-f0-9]{64}$/.test(pair.token) && pair.expires > Date.now();
}

async function stop() {
  await chrome.storage.session.remove(KEY);
  await chrome.alarms.clear(KEY);
}

async function sendCookies() {
  if (sending) return;
  sending = true;
  try {
    const pair = (await chrome.storage.session.get(KEY))[KEY];
    if (!validPair(pair)) { await stop(); return; }
    const cookies = await chrome.cookies.getAll({domain: "google.com", storeId: pair.storeId});
    const response = await fetch(`http://127.0.0.1:${pair.port}/session`, {
      method: "POST", cache: "no-store", credentials: "omit",
      headers: {"Content-Type": "application/json", "X-Gemini-Auth": pair.token},
      body: JSON.stringify({cookies: cookies.map(c => ({
        name: c.name, value: c.value, domain: c.domain, path: c.path, expires: c.expirationDate ?? -1
      }))}), signal: AbortSignal.timeout(5000)
    });
    if (!response.ok || (await response.json()).accepted) await stop();
  } catch (_) {
    await stop(); // The one-time terminal bridge has closed; never keep sending credentials.
  } finally {
    sending = false;
  }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type !== "pair" || sender.id !== chrome.runtime.id) return;
  let url;
  try { url = new URL(sender.url); } catch (_) { return; }
  const port = Number(message.port);
  const pageToken = new URLSearchParams(url.hash.slice(1)).get("gemini-login");
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/gemini-auth" ||
      Number(url.port) !== port || pageToken !== message.token || !sender.tab) return;
  const pair = {port, token: message.token, expires: Date.now() + 600000};
  if (!validPair(pair)) return;
  (async () => {
    const stores = await chrome.cookies.getAllCookieStores();
    pair.storeId = stores.find(s => s.tabIds.includes(sender.tab.id))?.id;
    if (!pair.storeId) throw new Error("No browser cookie store for this tab");
    await chrome.storage.session.set({[KEY]: pair});
    await chrome.alarms.create(KEY, {periodInMinutes: 0.5});
    await sendCookies();
    respond({ok: true});
  })().catch(() => respond({ok: false}));
  return true;
});
chrome.cookies.onChanged.addListener(change => {
  const domain = change.cookie.domain.replace(/^\./, "");
  if (domain === "google.com" || domain.endsWith(".google.com")) sendCookies();
});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === KEY) sendCookies(); });

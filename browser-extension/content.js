(() => {
  if (location.pathname !== "/gemini-auth" ||
      !document.querySelector('meta[name="gemini-terminal-login"][content="1"]')) return;
  const token = new URLSearchParams(location.hash.slice(1)).get("gemini-login");
  if (!/^[a-f0-9]{64}$/.test(token || "")) return;
  chrome.runtime.sendMessage({type: "pair", token, port: location.port}, result => {
    const label = document.getElementById("status");
    if (chrome.runtime.lastError || !result?.ok) {
      if (label) label.textContent = "连接失败，请确认终端仍在运行，然后重新打开终端输出的连接链接。";
      return;
    }
    location.replace("https://gemini.google.com/app");
  });
})();

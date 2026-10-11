(function () {
  var theme = "dark";
  try {
    var stored = localStorage.getItem("expolab.theme");
    if (stored === "light" || stored === "dark") theme = stored;
  } catch (e) { /* stockage indisponible : theme par defaut */ }
  document.documentElement.setAttribute("data-theme", theme);
})();

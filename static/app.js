document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

const fileInput = document.querySelector("#xlsx-file");
if (fileInput) {
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const form = fileInput.closest("form");
    const button = form.querySelector("button[type=submit], button:not([type])");
    const wasDisabled = button.disabled;
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      window.alert("Selecciona un archivo Excel con extensión .xlsx.");
      fileInput.value = "";
      return;
    }
    if (file.size > 5_000_000) {
      window.alert("El Excel supera el límite de 5 MB.");
      fileInput.value = "";
      return;
    }
    button.disabled = true;
    button.textContent = "Leyendo Excel…";
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    form.querySelector("input[name=xlsx_base64]").value = window.btoa(binary);
    form.querySelector("input[name=filename]").value = file.name;
    button.disabled = wasDisabled;
    button.textContent = "Revisar Excel";
  });
  fileInput.closest("form").addEventListener("submit", (event) => {
    if (!event.currentTarget.querySelector("input[name=xlsx_base64]").value) {
      event.preventDefault();
      window.alert("Espera a que el Excel termine de cargarse.");
    }
  });
}

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

const fileInput = document.querySelector("#csv-file");
if (fileInput) {
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    if (file.size > 2_000_000) {
      window.alert("El CSV supera el límite de 2 MB.");
      fileInput.value = "";
      return;
    }
    const target = fileInput.closest("form").querySelector("textarea[name=csv]");
    target.value = await file.text();
  });
}

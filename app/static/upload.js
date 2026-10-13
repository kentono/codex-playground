const input = document.querySelector('#image');
const preview = document.querySelector('#preview');
let previousUrl;
input.addEventListener('change', () => {
  if (previousUrl) URL.revokeObjectURL(previousUrl);
  const file = input.files[0];
  input.setCustomValidity(file && file.size > 10 * 1024 * 1024 ? 'Choose an image under 10 MB.' : '');
  preview.hidden = !file && !preview.dataset.currentSrc;
  document.querySelector('#upload-prompt').hidden = Boolean(file || preview.dataset.currentSrc);
  if (file) { previousUrl = URL.createObjectURL(file); preview.src = previousUrl; }
  else if (preview.dataset.currentSrc) { preview.src = preview.dataset.currentSrc; }
});

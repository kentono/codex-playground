const input = document.querySelector('#image');
const preview = document.querySelector('#preview');
let previousUrl;
input.addEventListener('change', () => {
  if (previousUrl) URL.revokeObjectURL(previousUrl);
  const file = input.files[0];
  input.setCustomValidity(file && file.size > 10 * 1024 * 1024 ? 'Choose an image under 10 MB.' : '');
  preview.hidden = !file;
  document.querySelector('#upload-prompt').hidden = Boolean(file);
  if (file) { previousUrl = URL.createObjectURL(file); preview.src = previousUrl; }
});

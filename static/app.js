const maxSizeMb = Number(document.body.dataset.maxSizeMb || '0');
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const fileName = document.getElementById('fileName');
const uploadBtn = document.getElementById('uploadBtn');
const uploadText = document.getElementById('uploadText');
const uploadSpinner = document.getElementById('uploadSpinner');
const messages = document.getElementById('messages');
const progressSection = document.getElementById('progressSection');
const resultSection = document.getElementById('resultSection');
const srtLink = document.getElementById('srtLink');
const assLink = document.getElementById('assLink');
const videoLink = document.getElementById('videoLink');
const backgroundOpacity = document.getElementById('backgroundOpacity');
const backgroundOpacityValue = document.getElementById('backgroundOpacityValue');

let selectedFile = null;
let statusPollTimer = null;

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    fileInput.click();
  }
});

dropZone.addEventListener('dragover', (event) => {
  event.preventDefault();
  dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));

dropZone.addEventListener('drop', (event) => {
  event.preventDefault();
  dropZone.classList.remove('dragover');
  if (event.dataTransfer.files.length) {
    selectFile(event.dataTransfer.files[0]);
  }
});

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) {
    selectFile(fileInput.files[0]);
  }
});

uploadBtn.addEventListener('click', upload);

if (backgroundOpacity && backgroundOpacityValue) {
  const syncOpacityLabel = () => {
    backgroundOpacityValue.textContent = `${backgroundOpacity.value}%`;
  };

  backgroundOpacity.addEventListener('input', syncOpacityLabel);
  syncOpacityLabel();
}

function selectFile(file) {
  selectedFile = file;
  fileName.textContent = `${file.name} (${(file.size / 1048576).toFixed(1)} MB)`;
  fileName.hidden = false;
  uploadBtn.disabled = false;
  resultSection.hidden = true;
  progressSection.hidden = true;
  srtLink.hidden = true;
  assLink.hidden = true;
  videoLink.hidden = true;
  clearMessages();
}

function clearMessages() {
  messages.replaceChildren();
}

function showMessage(text, type = 'info') {
  clearMessages();
  const box = document.createElement('div');
  box.className = `alert-box alert-${type}`;
  box.textContent = text;
  messages.appendChild(box);
}

function setUploading(isUploading) {
  uploadBtn.disabled = isUploading;
  uploadText.textContent = isUploading ? 'Processing...' : 'Generate Subtitles';
  uploadSpinner.hidden = !isUploading;
}

function updateBar(percent, text) {
  const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
  const bar = document.getElementById('bar');
  bar.style.width = `${safePercent}%`;
  bar.textContent = `${safePercent}%`;
  if (text) {
    document.getElementById('statusText').textContent = text;
  }
}

function buildResponseErrorMessage(response, fallbackText) {
  const statusLabel = response.status ? `Server error (${response.status})` : 'Server error';
  const trimmed = (fallbackText || '').trim();

  if (!trimmed) {
    return response.ok ? 'Server returned an empty response.' : `${statusLabel}. Please try again.`;
  }

  const compact = trimmed.replace(/\s+/g, ' ');
  const withoutHtml = compact.replace(/<[^>]+>/g, '').trim();
  const message = withoutHtml || compact;

  if (!response.ok) {
    return `${statusLabel}: ${message.slice(0, 180)}`;
  }

  return message.slice(0, 180);
}

async function parseJsonResponse(response) {
  const rawText = await response.text();

  if (!rawText) {
    throw new Error(buildResponseErrorMessage(response, ''));
  }

  try {
    return JSON.parse(rawText);
  } catch (_error) {
    throw new Error(buildResponseErrorMessage(response, rawText));
  }
}

async function upload() {
  if (!selectedFile) {
    return;
  }

  if (maxSizeMb > 0 && selectedFile.size > maxSizeMb * 1048576) {
    showMessage(`File too large. Maximum is ${maxSizeMb} MB.`, 'error');
    return;
  }

  clearMessages();
  setUploading(true);
  progressSection.hidden = false;
  resultSection.hidden = true;
  updateBar(0, 'Uploading...');

  const formData = new FormData();
  formData.append('file', selectedFile);
  appendSettings(formData);

  try {
    const response = await fetch('/', { method: 'POST', body: formData });
    const data = await parseJsonResponse(response);

    if (!response.ok || data.error) {
      throw new Error(data.error || 'Upload failed.');
    }

    if (!data.task_id) {
      throw new Error('Upload started, but the server did not return a task ID.');
    }

    updateBar(5, 'Processing started...');
    pollStatus(data.task_id);
  } catch (error) {
    showMessage(error.message || 'Network error.', 'error');
    setUploading(false);
    progressSection.hidden = true;
  }
}

function appendSettings(formData) {
  document.querySelectorAll('[data-upload-field]').forEach((field) => {
    if (!field.name) {
      return;
    }

    if (field.type === 'checkbox') {
      formData.append(field.name, field.checked ? '1' : '0');
      return;
    }

    formData.append(field.name, field.value ?? '');
  });
}

function pollStatus(taskId) {
  if (statusPollTimer) {
    clearInterval(statusPollTimer);
  }

  statusPollTimer = setInterval(async () => {
    try {
      const response = await fetch(`/status/${encodeURIComponent(taskId)}`);
      const data = await parseJsonResponse(response);

      if (!response.ok) {
        throw new Error(data.error || 'Unable to fetch task status.');
      }

      if (data.status === 'processing') {
        const progress = data.progress || 0;
        const message = progress < 15
          ? 'Loading model and starting transcription...'
          : progress < 80
            ? 'Transcribing speech...'
            : progress < 95
              ? 'Burning subtitles into video...'
              : 'Finalizing...';
        updateBar(progress, message);
        return;
      }

      if (data.status === 'done') {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
        updateBar(100, 'Complete!');
        setUploading(false);
        renderResult(data.result || {});
        return;
      }

      if (data.status === 'error' || data.status === 'unknown') {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
        throw new Error(data.error || 'Task was not found or failed.');
      }
    } catch (error) {
      if (statusPollTimer) {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
      }
      showMessage(error.message || 'Unable to fetch task status.', 'error');
      setUploading(false);
      progressSection.hidden = true;
    }
  }, 2000);
}

function renderResult(result) {
  resultSection.hidden = false;

  if (result.srt) {
    srtLink.href = `/download/${encodeURIComponent(result.srt)}`;
    srtLink.hidden = false;
  } else {
    srtLink.hidden = true;
  }

  if (result.ass) {
    assLink.href = `/download/${encodeURIComponent(result.ass)}`;
    assLink.hidden = false;
  } else {
    assLink.hidden = true;
  }

  if (result.video) {
    videoLink.href = `/download/${encodeURIComponent(result.video)}`;
    videoLink.hidden = false;
  } else {
    videoLink.hidden = true;
  }

  if (result.warning) {
    showMessage(result.warning, 'warning');
  }
}
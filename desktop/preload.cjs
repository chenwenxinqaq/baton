'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('umemDesktop', {
  getUpdateState: () => ipcRenderer.invoke('umem:update-state'),
  checkForUpdates: () => ipcRenderer.invoke('umem:update-check'),
  downloadUpdate: () => ipcRenderer.invoke('umem:update-download'),
  cancelDownload: () => ipcRenderer.invoke('umem:update-cancel'),
  installUpdate: () => ipcRenderer.invoke('umem:update-install'),
  skipUpdate: version => ipcRenderer.invoke('umem:update-skip', version),
  openReleasePage: url => ipcRenderer.invoke('umem:update-open-page', url),
  openUpdateLog: () => ipcRenderer.invoke('umem:update-open-log'),
  dismissNotice: () => ipcRenderer.invoke('umem:update-dismiss-notice'),
  copyText: text => ipcRenderer.invoke('umem:copy-text', String(text ?? '')),
  onUpdateState: listener => {
    if (typeof listener !== 'function') return () => {};
    const wrapped = (_event, state) => listener(state);
    ipcRenderer.on('umem:update-state', wrapped);
    return () => ipcRenderer.removeListener('umem:update-state', wrapped);
  },
});

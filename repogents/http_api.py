from __future__ import annotations

import json
import math
import re
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from repogents.errors import RepositoryLookupTimeoutError


_CLIENT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repogents</title>
<style>
/* Repogents interface system: locally served, dependency-free foundations. */
:root {
  color-scheme: dark;
  --font-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  --text-xs: .875rem;
  --text-sm: 1rem;
  --text-md: 1.25rem;
  --text-lg: 1.5rem;
  --text-xl: 2rem;
  --leading-tight: 1.25;
  --leading-normal: 1.5;
  --weight-medium: 600;
  --weight-bold: 700;
  --space-1: .25rem;
  --space-2: .5rem;
  --space-3: .75rem;
  --space-4: 1rem;
  --space-6: 1.5rem;
  --space-8: 2rem;
  --space-12: 3rem;
  --canvas: #0c1018;
  --surface: #111927;
  --surface-raised: #172235;
  --surface-muted: #1b2b42;
  --border: #657693;
  --border-strong: #7486a4;
  --text: #eef3ff;
  --text-secondary: #c8d2e4;
  --text-muted: #aebbd1;
  --accent: #8bb4ff;
  --accent-surface: #255ac7;
  --accent-hover: #1d459b;
  --focus: #ffd166;
  --neutral-fg: #d4dbea;
  --neutral-bg: #283346;
  --neutral-border: #7486a4;
  --active-fg: #cfe1ff;
  --active-bg: #173b70;
  --active-border: #5d91e5;
  --success-fg: #c8f5d7;
  --success-bg: #143d2a;
  --success-border: #4f9c6d;
  --warning-fg: #ffe6a3;
  --warning-bg: #493713;
  --warning-border: #b99137;
  --danger-fg: #ffd5da;
  --danger-bg: #4c2028;
  --danger-border: #c36a77;
  --radius-sm: .375rem;
  --radius-md: .625rem;
  --radius-pill: 999px;
  --control-height: 2.75rem;
  --content-max: 70rem;
  --shadow-raised: 0 .75rem 2rem rgb(0 0 0 / .24);
}
* { box-sizing: border-box; }
html { background: var(--canvas); }
body { margin: 0; min-width: 20rem; min-height: 100vh; background: var(--canvas); color: var(--text); font-family: var(--font-sans); font-size: var(--text-sm); line-height: var(--leading-normal); }
main { width: min(var(--content-max), calc(100% - 2rem)); margin-inline: auto; padding-block: var(--space-8) var(--space-12); }
h1, h2, h3, h4, h5, p { margin-block-start: 0; }
h1 { margin-block-end: var(--space-2); font-size: clamp(var(--text-xl), 5vw, 2.5rem); line-height: var(--leading-tight); letter-spacing: -.025em; }
h2 { margin-block-end: var(--space-3); font-size: var(--text-lg); line-height: var(--leading-tight); }
h3, h4, h5 { font-size: var(--text-md); line-height: var(--leading-tight); }
.lead { max-width: 48rem; margin-block-end: 0; color: var(--text-muted); }
.panel, .repo { border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); padding: var(--space-6); }
.dashboard-layout { display: grid; grid-template-areas: "repositories track"; grid-template-columns: minmax(0, 2fr) minmax(17rem, 1fr); gap: var(--space-6); align-items: start; margin-block-start: var(--space-8); }
.track-section { grid-area: track; }
.repository-section { grid-area: repositories; }
.repository-section, .repository-list { min-width: 0; }
.section-head { display: flex; align-items: end; justify-content: space-between; gap: var(--space-4); margin-block-end: var(--space-4); }
.section-head h2, .section-head p { margin-block-end: 0; }
.section-summary { color: var(--text-muted); }
.refresh-meta { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); color: var(--text-muted); font-size: var(--text-xs); }
.refresh-meta p { margin: 0; }
.repository-list { display: grid; gap: var(--space-4); }
.repo { box-shadow: var(--shadow-raised); }
.repo-head h3 { margin-block-end: var(--space-1); font-size: var(--text-lg); overflow-wrap: anywhere; }
.repo-content { display: grid; gap: var(--space-4); margin-block-start: var(--space-6); }
.repo-section { min-width: 0; }
.repo-section-title { display: block; margin-block-end: var(--space-2); color: var(--text-secondary); font-size: var(--text-xs); letter-spacing: .04em; text-transform: uppercase; }
.field-grid { display: grid; grid-template-columns: minmax(12rem, 2fr) minmax(10rem, 1fr) auto; gap: var(--space-4); align-items: end; }
.dashboard-layout > .panel .field-grid { grid-template-columns: 1fr; }
.dashboard-layout > .panel .field-grid button { width: 100%; }
.field { display: grid; gap: var(--space-2); min-width: 0; }
.field-label { color: var(--text-secondary); font-weight: var(--weight-medium); }
.field-hint { color: var(--text-muted); font-size: var(--text-xs); font-weight: 400; }
.field-error { min-width: 0; max-width: 100%; margin: 0; color: var(--danger-fg); font-size: var(--text-xs); overflow-wrap: anywhere; }
[aria-invalid="true"] { border-color: var(--danger-border); }
input, button { min-height: var(--control-height); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); color: inherit; font: inherit; }
input { width: 100%; min-width: 0; padding: var(--space-2) var(--space-3); background: var(--canvas); }
input::placeholder { color: var(--text-muted); opacity: 1; }
input:hover { border-color: var(--accent); }
button { padding: var(--space-2) var(--space-4); background: var(--accent-surface); border-color: var(--accent); cursor: pointer; font-weight: var(--weight-bold); }
button:hover { background: var(--accent-hover); }
button:active { transform: translateY(1px); }
button:disabled, input:disabled { cursor: not-allowed; opacity: .65; }
button.danger { background: transparent; border-color: var(--danger-border); color: var(--danger-fg); }
button.danger:hover { background: var(--danger-bg); }
a { color: var(--accent); text-decoration-thickness: .08em; text-underline-offset: .18em; }
a:hover { text-decoration-thickness: .14em; }
:where(a, button, input, summary):focus-visible { outline: .1875rem solid var(--focus); outline-offset: .1875rem; }
.repo-head, .run-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.repo-head > div { min-width: 0; }
.repo-remove { min-width: 0; max-width: 100%; white-space: normal; overflow-wrap: anywhere; text-align: center; }
.meta { color: var(--text-secondary); }
.code { font-family: var(--font-mono); font-size: .9375em; overflow-wrap: anywhere; }
.graph { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; margin-block: var(--space-3) 0; padding: 0; list-style: none; counter-reset: graph-step; }
.graph-step { display: flex; gap: var(--space-2); align-items: center; min-width: 0; counter-increment: graph-step; }
.node, .badge, .state { display: inline-flex; gap: var(--space-2); align-items: center; width: fit-content; border: 1px solid var(--neutral-border); border-radius: var(--radius-pill); background: var(--neutral-bg); color: var(--neutral-fg); padding: var(--space-1) var(--space-3); font-size: var(--text-xs); font-weight: var(--weight-medium); line-height: 1.4; overflow-wrap: anywhere; }
.node::before { content: counter(graph-step); display: inline-grid; place-items: center; min-width: 1.5rem; min-height: 1.5rem; border: 1px solid currentColor; border-radius: 50%; font-size: var(--text-xs); }
.node-copy { display: grid; }
.node-persistence { color: var(--text-secondary); font-size: var(--text-xs); font-weight: 400; }
.status-mark { font-weight: var(--weight-bold); }
.badge--active { border-color: var(--active-border); background: var(--active-bg); color: var(--active-fg); }
.badge--success { border-color: var(--success-border); background: var(--success-bg); color: var(--success-fg); }
.badge--warning { border-color: var(--warning-border); background: var(--warning-bg); color: var(--warning-fg); }
.badge--danger { border-color: var(--danger-border); background: var(--danger-bg); color: var(--danger-fg); }
.arrow { color: var(--text-muted); }
.run-list { display: grid; gap: var(--space-4); }
.run { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-raised); padding: var(--space-4); }
.run h5 { margin-block-end: var(--space-1); overflow-wrap: anywhere; }
.run-identity { min-width: 0; }
.issue-title { display: block; overflow-wrap: anywhere; }
.run-head .badge { flex: 0 0 auto; }
.run-meta { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); margin-block: var(--space-3) var(--space-4); }
.run-meta-item { min-width: 0; }
.columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.detail-group { min-width: 0; border-block-start: 1px solid var(--border); padding-block-start: var(--space-3); }
.detail-heading { display: flex; justify-content: space-between; gap: var(--space-2); }
.count { color: var(--text-muted); font-size: var(--text-xs); font-weight: 400; }
.detail-list { display: grid; gap: var(--space-2); margin-block: var(--space-2) 0; padding: 0; list-style: none; }
.detail-item { min-width: 0; border-inline-start: .1875rem solid var(--border-strong); padding-inline-start: var(--space-3); color: var(--text-secondary); overflow-wrap: anywhere; }
.detail-item-title { color: var(--text); font-weight: var(--weight-medium); }
.detail-item-meta { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-block-start: var(--space-1); }
.empty, .feedback { color: var(--text-muted); }
.empty-block { margin: 0; border: 1px dashed var(--border-strong); border-radius: var(--radius-sm); padding: var(--space-3); }
.feedback { min-width: 0; max-width: 100%; min-height: 1.5rem; margin-block: var(--space-2) 0; overflow-wrap: anywhere; }
.feedback:empty { min-height: 0; margin-block: 0; border: 0; background: transparent; padding: 0; }
.management-feedback { margin-block-end: var(--space-4); }
.feedback--error { color: var(--danger-fg); }
.feedback--warning { border-inline-start: .25rem solid var(--warning-border); background: var(--warning-bg); color: var(--warning-fg); padding: var(--space-3); }
.feedback--success { border-inline-start: .25rem solid var(--success-border); background: var(--success-bg); color: var(--success-fg); padding: var(--space-3); }
.skeleton { border-radius: var(--radius-sm); background: var(--surface-muted); color: transparent; }
@media (max-width: 55rem) {
  .dashboard-layout { grid-template-areas: "track" "repositories"; grid-template-columns: 1fr; }
}
@media (max-width: 45rem) {
  .field-grid, .columns { grid-template-columns: 1fr; }
  .field-grid button { width: 100%; }
  .section-head, .repo-head, .run-head { align-items: stretch; flex-direction: column; }
  .repo-head button { width: 100%; }
}
@media (max-width: 25rem) {
  main { width: min(var(--content-max), calc(100% - 2rem)); padding-block-start: var(--space-6); }
  .panel, .repo { padding: var(--space-4); }
  .dashboard-layout { gap: var(--space-4); margin-block-start: var(--space-6); }
  .graph, .graph-step { align-items: stretch; flex-direction: column; }
  .graph-step, .node { width: 100%; }
  .arrow { align-self: center; line-height: 1; transform: rotate(90deg); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
  button:active { transform: none; }
}
@media (forced-colors: active) {
  :where(.panel, .repo, .node, .badge, .state, input, button) { border-color: CanvasText; }
  :where(a, button, input, summary):focus-visible { outline-color: Highlight; }
}
</style>
</head>
<body><main>
<header><h1>Repogents</h1><p class="lead">Track repositories, inspect saved agent graphs, and follow issue automation from one operational view.</p></header>
<div class="dashboard-layout">
<aside class="panel track-section" aria-labelledby="track-heading"><h2 id="track-heading">Track repository</h2><p class="meta">Add a GitHub repository to monitor its agent graph and issue runs.</p>
<form id="add-form" class="field-grid" novalidate><label class="field" for="repository"><span class="field-label">GitHub repository <span class="field-hint">(required)</span></span><input id="repository" required placeholder="owner/repository" autocomplete="off" aria-describedby="repository-hint repository-error"><span id="repository-hint" class="field-hint">Use the owner/repository format.</span><span id="repository-error" class="field-error" role="alert" aria-atomic="true"></span></label><label class="field" for="branch"><span class="field-label">Target branch <span class="field-hint">(optional)</span></span><input id="branch" placeholder="Default branch from GitHub" autocomplete="off"></label><button id="add-button" type="submit">Add repository</button></form><div id="add-error" class="feedback feedback--error" role="alert"></div><div id="add-verification-status" class="feedback" role="status" aria-live="polite" aria-atomic="true"></div><div id="add-status" class="feedback feedback--success" role="status" aria-live="polite"></div></aside>
<section class="repository-section" aria-labelledby="repositories-heading">
<div class="section-head"><div><h2 id="repositories-heading">Tracked repositories</h2><p class="section-summary" id="repository-summary">Loading repository state…</p><div class="refresh-meta"><p id="freshness">Waiting for the first update</p><p id="refresh-status" role="status" aria-live="polite" aria-atomic="true"></p></div></div></div>
<div id="management-status" class="feedback feedback--success management-feedback" role="status" aria-live="polite"></div>
<div id="removal-announcement" class="feedback feedback--error" role="alert" aria-atomic="true"></div>
<div id="refresh-error" class="feedback feedback--warning management-feedback" role="alert" aria-atomic="true"></div>
<div id="repositories" class="repository-list" aria-busy="true"><p class="panel empty">Loading tracked repositories…</p></div>
</section>
</div>
</main>
<script>
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
class HttpApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}
async function api(path, options = {}) { const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options}); if (!response.ok) { const body = await response.json().catch(() => ({error: response.statusText})); throw new HttpApiError(body.error || response.statusText, response.status); } return response.status === 204 ? null : response.json(); }
const list = document.querySelector('#repositories');
const repositoryInput = document.querySelector('#repository');
const branchInput = document.querySelector('#branch');
const addForm = document.querySelector('#add-form');
const addButton = document.querySelector('#add-button');
let loadSequence = 0;
let mutationInProgress = false;
let hasRenderedState = false;
let lastStateSignature = '';
let lastKnownRepositories = [];
let lastSuccessfulRefresh = null;
let lastAnnouncedError = '';
let refreshTimer = null;
let activeRequest = null;
let stopped = false;
let lifecyclePaused = false;
let recoverableAddAttempt = null;
const removalErrors = new Map();
let removalAnnouncementRepositoryId = null;
const confirmedDeletions = new Set();
const REFRESH_INTERVAL = 3000;
const SLOW_REFRESH_DELAY = 750;
const STATE_REQUEST_TIMEOUT = 15000;
const ADD_REQUEST_TIMEOUT = 15000;
// Ambiguous POST outcomes remain mutation-owned until the durable server
// operation reaches COMMITTED or FAILED, including delays in the final store commit.
const ADD_OPERATION_STATUS_DELAY = 500;
const ADD_OPERATION_STATUS_TIMEOUT = 15000;
const ADD_OPERATION_MISSING_LIMIT = 3;
const ADD_OPERATION_REPLAY_LIMIT = 2;
const REMOVE_REQUEST_TIMEOUT = 15000;
function setText(selector, value) { document.querySelector(selector).textContent = value; }
function setRemoveControlsDisabled(disabled) {
  document.querySelectorAll('[data-remove]').forEach(control => {
    const repositoryId = String(control.dataset.remove);
    if (confirmedDeletions.has(repositoryId)) markConfirmedDeletion(repositoryId);
    else control.disabled = disabled;
  });
}
function setAddPending(pending) { mutationInProgress = pending; addForm.setAttribute('aria-busy', String(pending)); [...addForm.elements].forEach(control => control.disabled = pending); setRemoveControlsDisabled(pending); addButton.textContent = pending ? 'Adding repository…' : 'Add repository'; }
function clearRepositoryValidation() { repositoryInput.removeAttribute('aria-invalid'); setText('#repository-error', ''); }
function clearAddSubmissionError() { setText('#add-error', ''); }
function setAddVerificationStatus(message) {
  const region = document.querySelector('#add-verification-status');
  if (region && region.textContent !== message) region.textContent = message;
}
function clearAddVerificationStatus() { setAddVerificationStatus(''); }
function retireRecoverableAddAttempt(operationId) {
  if (recoverableAddAttempt && recoverableAddAttempt.operationId === operationId) {
    recoverableAddAttempt = null;
  }
}
function clearMutationStatuses() { setText('#add-status', ''); setText('#management-status', ''); }
function removalFeedbackRegion(repositoryId) { return document.querySelector(`#remove-feedback-${CSS.escape(String(repositoryId))}`); }
function clearRemovalAnnouncement(repositoryId) {
  const key = String(repositoryId);
  if (removalAnnouncementRepositoryId !== key) return;
  removalAnnouncementRepositoryId = null;
  const announcement = document.querySelector('#removal-announcement');
  if (announcement) announcement.textContent = '';
}
function clearRemovalError(repositoryId) {
  const key = String(repositoryId);
  removalErrors.delete(key);
  const feedback = removalFeedbackRegion(key);
  if (feedback) feedback.textContent = '';
  clearRemovalAnnouncement(key);
}
function setRemovalError(repositoryId, message) {
  const key = String(repositoryId);
  removalErrors.set(key, message);
  const feedback = removalFeedbackRegion(key);
  if (feedback) feedback.textContent = message;
  removalAnnouncementRepositoryId = key;
  const announcement = document.querySelector('#removal-announcement');
  if (announcement) announcement.textContent = message;
}
function restoreRemovalErrors(repositories) {
  const presentRepositoryIds = new Set(repositories.map(repo => String(repo.id)));
  for (const repositoryId of removalErrors.keys()) {
    if (!presentRepositoryIds.has(repositoryId)) {
      removalErrors.delete(repositoryId);
      clearRemovalAnnouncement(repositoryId);
    }
  }
  for (const repositoryId of presentRepositoryIds) {
    const feedback = removalFeedbackRegion(repositoryId);
    if (feedback) feedback.textContent = removalErrors.get(repositoryId) || '';
  }
}
function repositoryValidationMessage(value) { return value && /^[^/\s]+\/[^/\s]+$/.test(value) ? '' : 'Enter a GitHub repository in owner/repository format, for example acme/widget.'; }
function normalizedRepositoryIdentity(repository) { return String(repository || '').trim().toLowerCase(); }
function findTrackedRepository(repositories, repository) {
  const expected = normalizedRepositoryIdentity(repository);
  return repositories.find(item => normalizedRepositoryIdentity(item.github_repository) === expected) || null;
}
function repositoryIsTracked(repositories, repository) { return Boolean(findTrackedRepository(repositories, repository)); }
function alreadyTrackedMessage(repository, requestedBranch, trackedRepository) {
  const trackedBranch = String(trackedRepository.target_branch || '').trim();
  if (requestedBranch && trackedBranch && requestedBranch !== trackedBranch) {
    return `${repository} is already tracked on branch ${trackedBranch}. Adding it again would not change the tracked branch to ${requestedBranch}, so no add request was sent.`;
  }
  return `${repository} is already tracked${trackedBranch ? ` on branch ${trackedBranch}` : ''}, so no add request was sent.`;
}
function waitForAddOperationStatusDelay(delay = ADD_OPERATION_STATUS_DELAY) {
  return new Promise(resolve => setTimeout(resolve, delay));
}
function createRepositoryAddOperationId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `repogents-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
async function repositoryAddOperation(operationId) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), ADD_OPERATION_STATUS_TIMEOUT);
  try {
    return await api(`/api/repository-add-operations/${encodeURIComponent(operationId)}`, {
      signal: controller.signal
    });
  } catch (error) {
    // A received 404 authoritatively means this identity was never registered.
    // Transport failures, timeouts, and server unavailability remain non-outcomes.
    if (error instanceof HttpApiError && error.status === 404) return {state: 'MISSING'};
    return null;
  } finally {
    clearTimeout(timeout);
  }
}
async function replayRepositoryAddOperation(operationId, payload) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), ADD_REQUEST_TIMEOUT);
  try {
    const repository = await api('/api/repositories', {
      method: 'POST',
      body: JSON.stringify(payload),
      headers: {'Content-Type': 'application/json', 'X-Repogents-Operation-Id': operationId},
      signal: controller.signal
    });
    return {
      operation_id: operationId,
      github_repository: payload.github_repository,
      target_branch: payload.target_branch,
      state: 'COMMITTED',
      repository_id: repository && repository.id,
      error: null,
      repository
    };
  } catch (error) {
    // Even an authoritative replay error may have registered a terminal FAILED
    // operation. The status endpoint remains the only settlement authority.
    return null;
  } finally {
    clearTimeout(timeout);
  }
}
async function reconcileMissingAddOperation(repository, payload) {
  setAddVerificationStatus(`The add operation for ${repository} is no longer available. Checking current tracked repository state before any resend…`);
  const repositories = await load({background: hasRenderedState});
  if (repositories === false) return {state: 'STATE_UNAVAILABLE'};
  const trackedRepository = findTrackedRepository(repositories, repository);
  if (!trackedRepository) return {state: 'AUTHORITATIVELY_ABSENT', repositories};
  const requestedBranch = String(payload.target_branch || '').trim();
  const trackedBranch = String(trackedRepository.target_branch || '').trim();
  if (requestedBranch && requestedBranch !== trackedBranch) {
    return {
      state: 'TRACKED_DIFFERENT_BRANCH',
      repository: trackedRepository,
      repositories,
      requestedBranch,
      trackedBranch
    };
  }
  return {state: 'CURRENTLY_TRACKED', repository: trackedRepository, repositories};
}
async function waitForAuthoritativeAddCompletion(operationId, repository, payload) {
  let unavailable = false;
  let missingObservations = 0;
  let replayAttempts = 0;
  while (!stopped) {
    while ((lifecyclePaused || document.hidden) && !stopped) {
      await waitForAddOperationStatusDelay();
    }
    if (stopped) return {state: 'UNRESOLVED'};
    setAddVerificationStatus(unavailable
      ? `The server result for ${repository} is temporarily unavailable; Repogents will keep checking…`
      : `Waiting for the server to finish adding ${repository}…`);
    const operation = await repositoryAddOperation(operationId);
    if (operation && operation.state === 'COMMITTED') return operation;
    if (operation && operation.state === 'FAILED') return operation;
    if (operation && operation.state === 'MISSING') {
      missingObservations += 1;
      unavailable = false;
      const reconciliation = await reconcileMissingAddOperation(repository, payload);
      if (reconciliation.state === 'CURRENTLY_TRACKED'
          || reconciliation.state === 'TRACKED_DIFFERENT_BRANCH') {
        return reconciliation;
      }
      if (reconciliation.state === 'AUTHORITATIVELY_ABSENT'
          && replayAttempts < ADD_OPERATION_REPLAY_LIMIT) {
        replayAttempts += 1;
        setAddVerificationStatus(`Current repository state does not contain ${repository}. Repogents is safely resending the same operation identity…`);
        const replayed = await replayRepositoryAddOperation(operationId, payload);
        if (replayed && replayed.state === 'COMMITTED') return replayed;
      }
      if (missingObservations >= ADD_OPERATION_MISSING_LIMIT
          && (reconciliation.state === 'STATE_UNAVAILABLE'
              || replayAttempts >= ADD_OPERATION_REPLAY_LIMIT)) {
        return {state: 'MISSING_UNRESOLVED', stateReconciliation: reconciliation.state};
      }
    } else {
      unavailable = !operation;
    }
    await waitForAddOperationStatusDelay();
  }
  return {state: 'UNRESOLVED'};
}
function repositoryProjectionIsTracked(repository) {
  return Boolean(repository) && repository.tracked !== false;
}
function renderAuthoritativeAddedRepository(repository) {
  // Operation projections are historical completion evidence. They may safely
  // bridge an unavailable state request, but an explicitly untracked projection
  // removes any stale retained card instead of exposing a destructive action.
  if (!repository) return lastKnownRepositories;
  const existing = findTrackedRepository(lastKnownRepositories, repository.github_repository);
  const repositories = repositoryProjectionIsTracked(repository)
    ? (existing
      ? lastKnownRepositories.map(item => String(item.id) === String(existing.id) ? {...item, ...repository} : item)
      : [...lastKnownRepositories, {...repository, nodes: repository.nodes || [], runs: repository.runs || []}])
    : lastKnownRepositories.filter(item => normalizedRepositoryIdentity(item.github_repository) !== normalizedRepositoryIdentity(repository.github_repository));
  list.innerHTML = repositories.length
    ? repositories.map(renderRepository).join('')
    : '<p class="panel empty">No repositories are tracked yet. Use the form to add one.</p>';
  bindRemoveActions();
  restoreRemovalErrors(repositories);
  syncConfirmedDeletions(repositories);
  lastKnownRepositories = repositories;
  lastStateSignature = JSON.stringify(repositories);
  hasRenderedState = true;
  list.setAttribute('aria-busy', 'false');
  setText('#repository-summary', repositories.length === 1 ? '1 repository' : `${repositories.length} repositories`);
  return repositories;
}
async function settleCommittedAddOperation(operation) {
  const repositories = await load({background: hasRenderedState});
  if (repositories !== false) {
    // /api/state reads the same durable store after the COMMITTED status response.
    // Presence confirms the ordinary add; absence is authoritative evidence that
    // another client removed it later and must not be overwritten by history.
    if (!repositoryIsTracked(repositories, operation.github_repository)) {
      setText('#add-status', `${operation.github_repository} was added, but is no longer tracked.`);
    }
    return repositories;
  }
  // Only a genuinely unavailable state snapshot may fall back to the committed
  // operation projection. Explicit untracked metadata removes stale retained state.
  if (operation.repository && operation.repository.tracked === false) {
    setText('#add-status', `${operation.github_repository} was added, but is no longer tracked.`);
  }
  return renderAuthoritativeAddedRepository(operation.repository);
}
function showRepositoryValidationError(message) { repositoryInput.setAttribute('aria-invalid', 'true'); setText('#repository-error', message); repositoryInput.focus(); }
function restoreSuccessfulAddFocus(repository, repositories) {
  const trackedRepository = findTrackedRepository(repositories || [], repository);
  const heading = trackedRepository
    ? document.querySelector(`[data-repository-heading="${CSS.escape(String(trackedRepository.id))}"]`)
    : null;
  const target = heading || repositoryInput;
  if (heading && !heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
  target.focus({preventScroll: true});
}
const STATUS = {
  QUEUED: ['Queued', 'neutral', '○'], SPECIFYING: ['Creating specifications', 'active', '◉'], EXECUTING: ['Executing work', 'active', '◉'],
  WAITING_FOR_WORK_COMPLETION: ['Waiting for work', 'warning', '◷'], VALIDATING: ['Validating', 'active', '◉'], CREATING_PR: ['Creating pull request', 'active', '◉'],
  PR_LISTENING: ['Monitoring pull request', 'active', '◉'], OPEN: ['Open', 'active', '◉'], MERGED: ['Merged', 'success', '✓'], COMPLETED: ['Completed', 'success', '✓'], CLOSED: ['Closed', 'neutral', '■'],
  UNASSIGNED: ['Unassigned', 'neutral', '○'], RUNNING: ['Running', 'active', '◉'], HANDED_OFF: ['Handed off', 'success', '↗'], FAILED: ['Failed', 'danger', '!']
};
function statusBadge(value, noun = 'Status') { const key = String(value || 'UNKNOWN').toUpperCase(); const item = STATUS[key] || [key.toLowerCase().replaceAll('_', ' ').replace(/^./, x => x.toUpperCase()), 'neutral', '?']; const modifier = item[1] === 'neutral' ? '' : ` badge--${item[1]}`; return `<span class="badge${modifier}" aria-label="${esc(noun)}: ${esc(item[0])}"><span class="status-mark" aria-hidden="true">${item[2]}</span>${esc(item[0])}</span>`; }
function renderSpecification(spec) { return `<li class="detail-item"><span class="detail-item-title">${esc(spec.title || spec.key || 'Untitled specification')}</span>${spec.executable === false ? '<div class="detail-item-meta"><span class="badge"><span aria-hidden="true">◇</span>Planning only</span></div>' : ''}</li>`; }
function renderWorkItem(item) { return `<li class="detail-item"><span class="detail-item-title">${esc(item.title || item.key || 'Untitled work item')}</span><div class="detail-item-meta">${statusBadge(item.state, 'Work item status')}${item.classification ? `<span class="badge"><span aria-hidden="true">◆</span>${esc(item.classification)}</span>` : ''}</div></li>`; }
function renderRun(run, repositoryId) {
  const runId = `run-${esc(run.id ?? run.issue_number)}`;
  const specs = run.specifications || [];
  const work = run.work_items || [];
  const issueTitle = run.issue_json && run.issue_json.title ? `<span class="meta issue-title">${esc(run.issue_json.title)}</span>` : '';
  const pr = run.pull_request;
  const prFocusId = pr ? `${repositoryId}:${run.id ?? run.issue_number}:${pr.number}` : '';
  const prStatus = pr ? (pr.merged === true ? 'MERGED' : pr.state) : null;
  const prView = pr ? `<span><a href="${esc(pr.url)}" target="_blank" rel="noopener noreferrer" data-pr-focus="${esc(prFocusId)}" data-pr-repository="${esc(repositoryId)}" aria-label="Pull request #${esc(pr.number)} (opens in a new tab)">Pull request #${esc(pr.number)} <span aria-hidden="true">↗</span></a>${prStatus ? ` ${statusBadge(prStatus, 'Pull request status')}` : ''}</span>` : '<span class="meta">Pull request not created</span>';
  return `<article class="run" aria-labelledby="${runId}-heading"><div class="run-head"><div class="run-identity"><h5 id="${runId}-heading">Issue #${esc(run.issue_number)}</h5>${issueTitle}</div>${statusBadge(run.state, 'Run status')}</div><div class="run-meta"><span class="run-meta-item"><strong>Branch</strong> <span class="code">${esc(run.branch || 'Not created')}</span></span><span class="run-meta-item">${prView}</span></div><div class="columns"><section class="detail-group" aria-labelledby="${runId}-specifications"><strong id="${runId}-specifications" class="detail-heading"><span>Specifications</span><span class="count">${specs.length}</span></strong>${specs.length ? `<ul class="detail-list">${specs.map(renderSpecification).join('')}</ul>` : '<p class="empty empty-block">No specifications have been generated for this run yet.</p>'}</section><section class="detail-group" aria-labelledby="${runId}-work"><strong id="${runId}-work" class="detail-heading"><span>Work items</span><span class="count">${work.length}</span></strong>${work.length ? `<ul class="detail-list">${work.map(renderWorkItem).join('')}</ul>` : '<p class="empty empty-block">No work items have been created for this run yet.</p>'}</section></div></article>`;
}
function renderRepository(repo) {
  const headingId = `repository-${esc(repo.id)}-heading`;
  const graphLabelId = `repository-${esc(repo.id)}-graph`;
  const runLabelId = `repository-${esc(repo.id)}-runs`;
  const nodes = repo.nodes || [];
  const graph = nodes.length ? `<ol class="graph" role="list" aria-label="Agent graph sequence, ${nodes.length} ${nodes.length === 1 ? 'node' : 'nodes'}">${nodes.map((node, index) => `<li class="graph-step">${index ? '<span class="arrow" aria-hidden="true">→</span>' : ''}<span class="node"><span class="node-copy"><span>${esc(node.classification)}</span><span class="node-persistence">Persistence: ${esc(node.persistence)}</span></span></span></li>`).join('')}</ol>` : '<p class="empty empty-block">No saved agent graph nodes.</p>';
  const runs = repo.runs || [];
  const runView = runs.length ? `<div class="run-list">${runs.map(run => renderRun(run, repo.id)).join('')}</div>` : '<p class="empty empty-block">No issue runs are queued for this repository.</p>';
  return `<article class="repo" aria-labelledby="${headingId}"><div class="repo-head"><div><h3 id="${headingId}" data-repository-heading="${esc(repo.id)}" tabindex="-1">${esc(repo.github_repository)}</h3><p class="meta">Target branch <span class="code">${esc(repo.target_branch)}</span></p></div><button class="danger repo-remove" data-remove="${esc(repo.id)}" data-repository="${esc(repo.github_repository)}" data-remove-focus="${esc(repo.id)}" aria-describedby="remove-feedback-${esc(repo.id)}">Remove repository<span class="field-hint"> ${esc(repo.github_repository)}</span></button></div><div id="remove-feedback-${esc(repo.id)}" class="feedback feedback--error"></div><div class="repo-content"><section class="repo-section" aria-labelledby="${graphLabelId}"><h4 id="${graphLabelId}" class="repo-section-title">Saved agent graph</h4>${graph}</section><section class="repo-section" aria-labelledby="${runLabelId}"><h4 id="${runLabelId}" class="repo-section-title">Issue runs <span class="count">${runs.length}</span></h4>${runView}</section></div></article>`;
}
function markConfirmedDeletion(repositoryId) {
  const button = document.querySelector(`[data-remove="${CSS.escape(String(repositoryId))}"]`);
  if (!button) return;
  button.disabled = true;
  button.textContent = 'Removal confirmed — refreshing repository state…';
}
function syncConfirmedDeletions(repositories) {
  const presentRepositoryIds = new Set(repositories.map(repo => String(repo.id)));
  for (const repositoryId of [...confirmedDeletions]) {
    if (!presentRepositoryIds.has(repositoryId)) confirmedDeletions.delete(repositoryId);
  }
  for (const repositoryId of confirmedDeletions) markConfirmedDeletion(repositoryId);
}
function bindRemoveActions() { document.querySelectorAll('[data-remove]').forEach((button, index, buttons) => { button.disabled = mutationInProgress || confirmedDeletions.has(String(button.dataset.remove)); button.addEventListener('click', () => removeRepository(button, index, buttons.length)); }); }
function updateFreshness() {
  if (!lastSuccessfulRefresh) return;
  const seconds = Math.max(0, Math.floor((Date.now() - lastSuccessfulRefresh) / 1000));
  setText('#freshness', seconds < 10 ? 'Updated just now' : `Updated ${seconds} seconds ago`);
}
function scheduleRefresh(delay = REFRESH_INTERVAL) {
  clearTimeout(refreshTimer);
  if (stopped || lifecyclePaused) return;
  refreshTimer = setTimeout(async () => {
    updateFreshness();
    if (!mutationInProgress && !document.hidden) await load({background: true});
    scheduleRefresh();
  }, delay);
}
function captureRefreshFocus() {
  const activeElement = document.activeElement;
  if (!activeElement || !activeElement.dataset) return null;
  if (activeElement.dataset.removeFocus) {
    const controls = [...document.querySelectorAll('[data-remove-focus]')];
    return {kind: 'remove', id: activeElement.dataset.removeFocus, repositoryIndex: controls.indexOf(activeElement)};
  }
  if (activeElement.dataset.prFocus) return {kind: 'pull-request', id: activeElement.dataset.prFocus, repositoryId: activeElement.dataset.prRepository};
  if (activeElement.dataset.repositoryHeading) {
    const headings = [...document.querySelectorAll('[data-repository-heading]')];
    return {kind: 'repository-heading', id: activeElement.dataset.repositoryHeading, repositoryIndex: headings.indexOf(activeElement)};
  }
  return null;
}
function restoreRefreshFocus(focusTarget, focusIndex) {
  let target = null;
  if (focusTarget && focusTarget.kind === 'remove') {
    target = document.querySelector(`[data-remove-focus="${CSS.escape(focusTarget.id)}"]`);
    if (!target) {
      const repositoryHeadings = [...document.querySelectorAll('[data-repository-heading]')];
      target = repositoryHeadings[Math.min(focusTarget.repositoryIndex, repositoryHeadings.length - 1)] || document.querySelector('#repositories-heading');
      if (target && !target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    }
  }
  if (focusTarget && focusTarget.kind === 'pull-request') {
    target = document.querySelector(`[data-pr-focus="${CSS.escape(focusTarget.id)}"]`);
    if (!target) {
      target = document.querySelector(`#repository-${CSS.escape(focusTarget.repositoryId)}-heading`) || document.querySelector('#repositories-heading');
      if (target && !target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    }
  }
  if (focusTarget && focusTarget.kind === 'repository-heading') {
    target = document.querySelector(`[data-repository-heading="${CSS.escape(focusTarget.id)}"]`);
    if (!target) {
      const repositoryHeadings = [...document.querySelectorAll('[data-repository-heading]')];
      target = repositoryHeadings[Math.min(focusTarget.repositoryIndex, repositoryHeadings.length - 1)] || document.querySelector('#repositories-heading');
    }
    if (target && !target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
  }
  if (!target && focusIndex !== null) {
    const buttons = [...document.querySelectorAll('[data-remove]')];
    target = buttons[Math.min(focusIndex, buttons.length - 1)] || document.querySelector('#repositories-heading');
    if (!buttons.length) target.setAttribute('tabindex', '-1');
  }
  if (target) target.focus({preventScroll: true});
}
function restoreSuccessfulRemovalFocus(focusIndex) {
  const buttons = [...document.querySelectorAll('[data-remove]')];
  let target = buttons[Math.min(focusIndex, buttons.length - 1)] || null;
  if (target && target.disabled) {
    target = buttons.slice(focusIndex).find(control => !control.disabled)
      || buttons.slice(0, focusIndex).reverse().find(control => !control.disabled)
      || null;
  }
  if (!target) {
    const repositoryHeadings = [...document.querySelectorAll('[data-repository-heading]')];
    target = repositoryHeadings[Math.min(focusIndex, repositoryHeadings.length - 1)]
      || document.querySelector('#repositories-heading');
    if (target && !target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
  }
  if (target) target.focus({preventScroll: true});
}
async function load({focusIndex = null, background = false} = {}) {
  const requestId = ++loadSequence;
  if (activeRequest) activeRequest.abort();
  const controller = new AbortController();
  activeRequest = controller;
  const refreshFocusTarget = captureRefreshFocus();
  const initial = !hasRenderedState;
  if (initial) list.setAttribute('aria-busy', 'true');
  const slowNotice = setTimeout(() => {
    if (requestId === loadSequence) setText('#refresh-status', initial ? 'Still loading repository state…' : 'Refreshing in the background…');
  }, SLOW_REFRESH_DELAY);
  let requestTimedOut = false;
  const requestTimeout = setTimeout(() => {
    requestTimedOut = true;
    controller.abort();
  }, STATE_REQUEST_TIMEOUT);
  try {
    const state = await api('/api/state', {signal: controller.signal});
    if (requestId !== loadSequence || stopped || lifecyclePaused) return false;
    const repositories = state.repositories || [];
    lastKnownRepositories = repositories;
    const signature = JSON.stringify(repositories);
    const changed = signature !== lastStateSignature;
    if (initial || changed) {
      list.innerHTML = repositories.length ? repositories.map(renderRepository).join('') : '<p class="panel empty">No repositories are tracked yet. Use the form to add one.</p>';
      bindRemoveActions();
      restoreRemovalErrors(repositories);
      syncConfirmedDeletions(repositories);
      lastStateSignature = signature;
      hasRenderedState = true;
      restoreRefreshFocus(refreshFocusTarget, focusIndex);
    } else {
      syncConfirmedDeletions(repositories);
      if (focusIndex !== null) restoreRefreshFocus(null, focusIndex);
    }
    list.setAttribute('aria-busy', 'false');
    setText('#repository-summary', repositories.length === 1 ? '1 repository' : `${repositories.length} repositories`);
    const recovered = Boolean(lastAnnouncedError);
    setText('#refresh-error', '');
    lastAnnouncedError = '';
    lastSuccessfulRefresh = Date.now();
    updateFreshness();
    setText('#refresh-status', recovered ? 'Repository updates resumed.' : (background && changed ? 'Repository state updated.' : ''));
    return repositories;
  } catch (error) {
    if (requestId !== loadSequence || (!requestTimedOut && error.name === 'AbortError') || stopped || lifecyclePaused) return false;
    list.setAttribute('aria-busy', 'false');
    const errorDetail = requestTimedOut ? 'The request timed out' : error.message;
    const message = initial
      ? `Repository state could not be loaded: ${errorDetail}. Repogents will retry automatically.`
      : `Updates are temporarily unavailable: ${errorDetail}. Showing the last successful repository state, which may be outdated. Repogents will retry automatically.`;
    if (initial) {
      list.innerHTML = '<div class="panel empty"><strong>Repository state unavailable</strong><p>Tracked repositories could not be loaded. Repogents will retry automatically.</p></div>';
      setText('#repository-summary', 'Repository state unavailable');
      setText('#freshness', 'No repository update available');
    }
    if (message !== lastAnnouncedError) {
      setText('#refresh-error', message);
      lastAnnouncedError = message;
    }
    setText('#refresh-status', '');
    updateFreshness();
    return false;
  } finally {
    clearTimeout(slowNotice);
    clearTimeout(requestTimeout);
    if (requestId === loadSequence) activeRequest = null;
  }
}
async function removeRepository(button, index) {
  const repositoryId = String(button.dataset.remove);
  if (mutationInProgress || confirmedDeletions.has(repositoryId)) return;
  const name = button.dataset.repository;
  if (!window.confirm(`Remove ${name} from Repogents? This stops tracking it but does not delete the GitHub repository.`)) return;
  mutationInProgress = true;
  ++loadSequence;
  if (activeRequest) activeRequest.abort();
  [...addForm.elements].forEach(control => control.disabled = true);
  document.querySelectorAll('[data-remove]').forEach(control => control.disabled = true);
  const originalLabel = button.innerHTML;
  button.disabled = true;
  button.textContent = 'Removing repository…';
  clearMutationStatuses();
  clearRemovalError(repositoryId);
  const removeController = new AbortController();
  let removeTimedOut = false;
  let successfulRemovalFocusIndex = null;
  const removeTimeout = setTimeout(() => {
    removeTimedOut = true;
    removeController.abort();
  }, REMOVE_REQUEST_TIMEOUT);
  try {
    await api(`/api/repositories/${encodeURIComponent(repositoryId)}`, {
      method: 'DELETE',
      signal: removeController.signal
    });
    clearTimeout(removeTimeout);
    clearRemovalError(repositoryId);
    confirmedDeletions.add(repositoryId);
    markConfirmedDeletion(repositoryId);
    setText('#management-status', `${name} was removed from tracked repositories.`);
    const reconciledRepositories = await load();
    if (reconciledRepositories) successfulRemovalFocusIndex = index;
    else {
      markConfirmedDeletion(repositoryId);
      const focusTarget = document.querySelector(`#repository-${CSS.escape(repositoryId)}-heading`) || document.querySelector('#repositories-heading');
      if (focusTarget) focusTarget.focus({preventScroll: true});
    }
  } catch (error) {
    const transportOutcomeUncertain = !removeTimedOut && !(error instanceof HttpApiError);
    if (transportOutcomeUncertain) {
      const reconciledRepositories = await load({background: hasRenderedState});
      if (reconciledRepositories && !reconciledRepositories.some(repo => String(repo.id) === repositoryId)) {
        clearRemovalError(repositoryId);
        setText('#management-status', `${name} was removed from tracked repositories.`);
        successfulRemovalFocusIndex = index;
      } else if (reconciledRepositories) {
        const message = `Removal of ${name} was not observed in the latest tracked repository state. You can try removing it again.`;
        setRemovalError(repositoryId, message);
        const currentButton = document.querySelector(`[data-remove="${CSS.escape(repositoryId)}"]`) || button;
        currentButton.disabled = false;
        currentButton.innerHTML = originalLabel;
        currentButton.focus({preventScroll: true});
      } else {
        const message = `Could not confirm whether ${name} was removed because the connection ended before Repogents received a response and current repository state could not be loaded. Check the tracked repository list before trying again.`;
        setRemovalError(repositoryId, message);
        const currentButton = document.querySelector(`[data-remove="${CSS.escape(repositoryId)}"]`) || button;
        currentButton.disabled = false;
        currentButton.innerHTML = originalLabel;
        currentButton.focus({preventScroll: true});
      }
    } else {
      const message = removeTimedOut
        ? `Could not confirm whether ${name} was removed because the request timed out. Check the tracked repository list before trying again.`
        : `Could not remove ${name}: ${error.message}. The repository is still tracked; try again.`;
      setRemovalError(repositoryId, message);
      button.disabled = false;
      button.innerHTML = originalLabel;
      button.focus();
    }
  } finally {
    clearTimeout(removeTimeout);
    mutationInProgress = false;
    [...addForm.elements].forEach(control => control.disabled = false);
    document.querySelectorAll('[data-remove]').forEach(control => control.disabled = confirmedDeletions.has(String(control.dataset.remove)));
    if (successfulRemovalFocusIndex !== null) restoreSuccessfulRemovalFocus(successfulRemovalFocusIndex);
  }
}
addForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (mutationInProgress) return;
  const repository = repositoryInput.value.trim();
  const branch = branchInput.value.trim();
  clearRepositoryValidation();
  clearAddSubmissionError();
  const validationMessage = repositoryValidationMessage(repository);
  if (validationMessage) { showRepositoryValidationError(validationMessage); return; }
  const preexistingRepository = findTrackedRepository(lastKnownRepositories, repository);
  if (preexistingRepository) {
    showRepositoryValidationError(alreadyTrackedMessage(repository, branch, preexistingRepository));
    return;
  }
  clearMutationStatuses();
  clearAddVerificationStatus();
  setAddPending(true);
  ++loadSequence;
  if (activeRequest) activeRequest.abort();
  const addPayload = {github_repository: repository, target_branch: branch || null};
  const addPayloadKey = `${normalizedRepositoryIdentity(repository)}\n${branch}`;
  const addOperationId = recoverableAddAttempt && recoverableAddAttempt.payloadKey === addPayloadKey
    ? recoverableAddAttempt.operationId
    : createRepositoryAddOperationId();
  if (recoverableAddAttempt && recoverableAddAttempt.payloadKey !== addPayloadKey) recoverableAddAttempt = null;
  const addController = new AbortController();
  let addTimedOut = false;
  let restoreRepositoryFocus = false;
  let successfulAdditionRepositories = null;
  const addTimeout = setTimeout(() => {
    addTimedOut = true;
    addController.abort();
  }, ADD_REQUEST_TIMEOUT);
  try {
    const addedRepository = await api('/api/repositories', {
      method: 'POST',
      body: JSON.stringify(addPayload),
      headers: {'Content-Type': 'application/json', 'X-Repogents-Operation-Id': addOperationId},
      signal: addController.signal
    });
    clearTimeout(addTimeout);
    recoverableAddAttempt = null;
    addForm.reset();
    clearAddVerificationStatus();
    setText('#add-status', `${repository} was added to tracked repositories.`);
    const repositories = await load();
    if (repositories !== false) {
      successfulAdditionRepositories = repositories;
      if (!repositoryIsTracked(repositories, repository)) {
        setText('#add-status', `${repository} was added, but is no longer tracked.`);
      }
    } else {
      if (addedRepository && addedRepository.tracked === false) {
        setText('#add-status', `${repository} was added, but is no longer tracked.`);
      }
      successfulAdditionRepositories = renderAuthoritativeAddedRepository(addedRepository);
    }
  } catch (error) {
    const authoritativeTerminalFailure = error instanceof HttpApiError;
    const authoritativeNoCommit = authoritativeTerminalFailure && error.status === 504;
    const transportOutcomeUncertain = addTimedOut || !authoritativeTerminalFailure;
    // Any received POST error is authoritative for this request identity: the
    // server has either rejected it before commit or durably recorded FAILED.
    // Retire only the matching cached recovery attempt before exposing retry
    // guidance, so a later unchanged user retry creates a viable new operation.
    if (authoritativeTerminalFailure) retireRecoverableAddAttempt(addOperationId);
    if (authoritativeNoCommit) {
      clearAddVerificationStatus();
      setText('#add-error', `Could not add ${repository}: the GitHub repository lookup timed out before Repogents could commit it. The repository was not added, and it is safe to try again.`);
      restoreRepositoryFocus = true;
    } else if (transportOutcomeUncertain) {
      const uncertaintyReason = addTimedOut
        ? `The browser request to add ${repository} timed out, so its response is unknown.`
        : `The connection ended before Repogents received a response for ${repository}, so its response is unknown.`;
      setAddVerificationStatus(`${uncertaintyReason} Checking the server's authoritative operation result…`);
      const operation = await waitForAuthoritativeAddCompletion(addOperationId, repository, addPayload);
      if (operation.state === 'COMMITTED') {
        recoverableAddAttempt = null;
        clearAddVerificationStatus();
        addForm.reset();
        clearAddSubmissionError();
        setText('#add-status', `${repository} was added to tracked repositories.`);
        successfulAdditionRepositories = await settleCommittedAddOperation(operation);
      } else if (operation.state === 'CURRENTLY_TRACKED') {
        recoverableAddAttempt = null;
        clearAddVerificationStatus();
        addForm.reset();
        clearAddSubmissionError();
        setText('#add-status', `${repository} is currently tracked${operation.repository.target_branch ? ` on branch ${operation.repository.target_branch}` : ''}. No duplicate add request was sent.`);
        successfulAdditionRepositories = operation.repositories;
      } else if (operation.state === 'TRACKED_DIFFERENT_BRANCH') {
        recoverableAddAttempt = null;
        clearAddVerificationStatus();
        setText('#add-error', `${repository} is currently tracked on branch ${operation.trackedBranch}, not the requested branch ${operation.requestedBranch}. Repogents did not resend the expired add operation.`);
        restoreRepositoryFocus = true;
      } else if (operation.state === 'FAILED') {
        recoverableAddAttempt = null;
        clearAddVerificationStatus();
        setText('#add-error', `${repository} was not added. The server confirmed that the original operation failed and cannot commit later${operation.error ? `: ${operation.error}` : '.'} It is safe to try again.`);
        restoreRepositoryFocus = true;
      } else if (operation.state === 'MISSING_UNRESOLVED') {
        clearAddVerificationStatus();
        recoverableAddAttempt = {operationId: addOperationId, payloadKey: addPayloadKey};
        const stateDetail = operation.stateReconciliation === 'STATE_UNAVAILABLE'
          ? 'current tracked repository state was unavailable'
          : 'the repository remained absent after bounded authoritative checks';
        setText('#add-error', `Repogents could not confirm the expired add operation for ${repository} because ${stateDetail}. Your values are retained. Check the tracked repository state before trying again; an unchanged retry will reuse the same operation identity, and Repogents did not treat a missing operation record as proof that replay was safe.`);
        restoreRepositoryFocus = true;
      }
    } else {
      clearAddVerificationStatus();
      setText('#add-error', `Could not add ${repository}: ${error.message}. Check the repository and branch, then try again.`);
      restoreRepositoryFocus = true;
    }
  } finally {
    clearTimeout(addTimeout);
    setAddPending(false);
    if (successfulAdditionRepositories) restoreSuccessfulAddFocus(repository, successfulAdditionRepositories);
    else if (restoreRepositoryFocus) repositoryInput.focus();
  }
});
repositoryInput.addEventListener('input', () => { if (repositoryInput.hasAttribute('aria-invalid')) clearRepositoryValidation(); });
load().finally(() => scheduleRefresh());
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(refreshTimer); }
  else if (mutationInProgress) { scheduleRefresh(); }
  else { load({background: hasRenderedState}).finally(() => scheduleRefresh()); }
});
window.addEventListener('pagehide', event => {
  clearTimeout(refreshTimer);
  refreshTimer = null;
  ++loadSequence;
  if (activeRequest) activeRequest.abort();
  activeRequest = null;
  if (event && event.persisted) {
    lifecyclePaused = true;
    return;
  }
  stopped = true;
});
window.addEventListener('pageshow', event => {
  if (!event || !event.persisted) return;
  stopped = false;
  lifecyclePaused = false;
  if (document.hidden) return;
  if (mutationInProgress) { scheduleRefresh(); return; }
  load({background: hasRenderedState}).finally(() => scheduleRefresh());
});
</script></body></html>"""


class _AbsoluteDeadlineReader:
    """Expose buffered request input while enforcing one monotonic deadline.

    ``socket.settimeout`` only limits one low-level operation.  ``readline`` on a
    buffered socket can perform many such operations, so a slow client can renew
    that inactivity timeout indefinitely.  This wrapper performs at most one raw
    buffered read per iteration and reapplies only the remaining request budget.
    Bytes read beyond a line boundary remain locally buffered for header or body
    parsing, preserving normal ``BaseHTTPRequestHandler`` behavior.
    """

    def __init__(self, reader, connection, deadline):
        self._reader = reader
        self._connection = connection
        self._deadline = deadline
        self._buffer = bytearray()

    def _remaining(self) -> float:
        remaining = self._deadline() - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("request input timed out")
        self._connection.settimeout(remaining)
        return remaining

    def _read_once(self, size: int) -> bytes:
        self._remaining()
        return self._reader.read1(max(1, size))

    def readline(self, limit: int = -1) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            available = len(self._buffer)
            if newline >= 0 or (limit >= 0 and available >= limit):
                end = newline + 1 if newline >= 0 else available
                if limit >= 0:
                    end = min(end, limit)
                result = bytes(self._buffer[:end])
                del self._buffer[:end]
                return result
            read_size = 64 * 1024 if limit < 0 else min(64 * 1024, limit - available)
            chunk = self._read_once(read_size)
            if not chunk:
                result = bytes(self._buffer)
                self._buffer.clear()
                return result
            self._buffer.extend(chunk)

    def read1(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if self._buffer:
            end = len(self._buffer) if size < 0 else min(size, len(self._buffer))
            result = bytes(self._buffer[:end])
            del self._buffer[:end]
            return result
        return self._read_once(64 * 1024 if size < 0 else size)

    def __getattr__(self, name):
        return getattr(self._reader, name)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunks = [bytes(self._buffer)]
            self._buffer.clear()
            while True:
                chunk = self._read_once(64 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        chunks = []
        remaining = size
        while remaining:
            chunk = self.read1(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class _LifecycleThreadingHTTPServer(ThreadingHTTPServer):
    """Join every accepted request handler before service ownership can close.

    ``ThreadingHTTPServer`` normally marks request threads as daemons. Its
    ``server_close`` tracking therefore does not retain them, allowing a handler
    that is blocked in application or storage code to outlive the listener and the
    data-directory ownership lock. Non-daemon request threads are tracked by
    ``ThreadingMixIn`` and joined by ``server_close`` when ``block_on_close`` is true.
    """

    daemon_threads = False
    block_on_close = True

    def __init__(self, server_address, handler_class, *, request_io_timeout: float):
        self.request_io_timeout = request_io_timeout
        super().__init__(server_address, handler_class)

    def get_request(self):
        request, client_address = super().get_request()
        # Install the deadline before request-line/header parsing begins, so every
        # accepted connection has bounded input and output I/O.
        request.settimeout(self.request_io_timeout)
        return request, client_address


class HttpService:
    def __init__(
        self, application, host: str, port: int, poll_seconds: float,
        request_io_timeout: float = 30.0,
    ):
        if not math.isfinite(request_io_timeout) or request_io_timeout <= 0:
            raise ValueError("request_io_timeout must be finite and positive")
        self.application = application
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def handle_one_request(self) -> None:
                """Parse and dispatch one request under one absolute input deadline."""
                self._request_input_deadline = (
                    time.monotonic() + service._server.request_io_timeout
                )
                if not isinstance(self.rfile, _AbsoluteDeadlineReader):
                    self.rfile = _AbsoluteDeadlineReader(
                        self.rfile, self.connection,
                        lambda: self._request_input_deadline,
                    )
                try:
                    self.raw_requestline = self.rfile.readline(65537)
                    if len(self.raw_requestline) > 65536:
                        self.requestline = ""
                        self.request_version = ""
                        self.command = ""
                        self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                        return
                    if not self.raw_requestline:
                        self.close_connection = True
                        return
                    if not self.parse_request():
                        return
                    method_name = "do_" + self.command
                    if not hasattr(self, method_name):
                        self.send_error(
                            HTTPStatus.NOT_IMPLEMENTED,
                            f"Unsupported method ({self.command!r})",
                        )
                        return
                    getattr(self, method_name)()
                    self.wfile.flush()
                except TimeoutError as error:
                    # Before a complete request line there is no reliable HTTP
                    # version to answer.  Once parsing has established one, send a
                    # bounded 408 when the peer is still writable.
                    self.close_connection = True
                    if getattr(self, "request_version", ""):
                        try:
                            self._error(
                                HTTPStatus.REQUEST_TIMEOUT, "request input timed out"
                            )
                        except OSError:
                            pass
                    self.log_error("Request timed out: %r", error)
                finally:
                    try:
                        self.connection.settimeout(service._server.request_io_timeout)
                    except OSError:
                        pass

            def _send_json(self, status: int, value, headers=None) -> None:
                body = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if headers:
                    for name, header_value in headers.items():
                        self.send_header(name, header_value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status: int, error: Exception | str) -> None:
                self._send_json(status, {"error": str(error)})

            def _read_request_body(self, length: int) -> bytes:
                """Read one declared body under a total monotonic deadline.

                A socket timeout applies separately to each recv, so a client can
                otherwise keep a handler alive forever by sending one byte before
                every timeout. ``BufferedReader.read1`` performs at most one raw
                socket read per call; recalculating the remaining budget before
                each call makes the configured request-I/O timeout an absolute
                body deadline rather than a renewable inactivity timeout.
                """
                if length < 0:
                    raise ValueError("Content-Length must be nonnegative")
                deadline = self._request_input_deadline
                chunks = []
                remaining = length
                try:
                    while remaining:
                        seconds_left = deadline - time.monotonic()
                        if seconds_left <= 0:
                            raise socket.timeout("request body timed out")
                        self.connection.settimeout(seconds_left)
                        chunk = self.rfile.read1(min(remaining, 64 * 1024))
                        if not chunk:
                            raise ValueError("request body ended before Content-Length")
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    return b"".join(chunks)
                finally:
                    # Complete requests retain the configured finite per-operation
                    # transport bound for response I/O and any subsequent request.
                    self.connection.settimeout(service._server.request_io_timeout)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/":
                    body = _CLIENT_HTML.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/state":
                    self._send_json(HTTPStatus.OK, service.application.state())
                    return
                operation_prefix = "/api/repository-add-operations/"
                if path.startswith(operation_prefix):
                    encoded_operation_id = path[len(operation_prefix) :]
                    if not encoded_operation_id:
                        self._error(HTTPStatus.BAD_REQUEST, "operation id is required")
                        return
                    if re.search(r"%(?![0-9A-Fa-f]{2})", encoded_operation_id):
                        self._error(HTTPStatus.BAD_REQUEST, "operation id has invalid URL encoding")
                        return
                    try:
                        operation_id = unquote(
                            encoded_operation_id, encoding="utf-8", errors="strict"
                        )
                    except UnicodeDecodeError:
                        self._error(HTTPStatus.BAD_REQUEST, "operation id has invalid URL encoding")
                        return
                    if (
                        not operation_id
                        or operation_id != operation_id.strip()
                        or len(operation_id) > 200
                    ):
                        self._error(
                            HTTPStatus.BAD_REQUEST,
                            "operation id must be 1 to 200 non-whitespace-surrounded characters",
                        )
                        return
                    operation = service.application.repository_add_operation(operation_id)
                    if operation is None:
                        self._error(HTTPStatus.NOT_FOUND, "repository add operation not found")
                    else:
                        self._send_json(HTTPStatus.OK, operation)
                    return
                self._error(HTTPStatus.NOT_FOUND, "not found")

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/api/repositories":
                    self._error(HTTPStatus.NOT_FOUND, "not found")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    try:
                        body = self._read_request_body(length)
                    except socket.timeout:
                        # Scope HTTP 408 handling to request input only. Application
                        # timeouts have their own authoritative transport semantics.
                        self.close_connection = True
                        try:
                            self._error(HTTPStatus.REQUEST_TIMEOUT, "request body timed out")
                        except OSError:
                            pass
                        return
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be an object")
                    repository = payload.get("github_repository")
                    if not isinstance(repository, str) or not repository.strip():
                        raise ValueError("github_repository is required")
                    target_branch = payload.get("target_branch")
                    if target_branch is not None and (
                        not isinstance(target_branch, str) or not target_branch.strip()
                    ):
                        raise ValueError("target_branch must be a nonempty string or null")
                    operation_id = self.headers.get("X-Repogents-Operation-Id")
                    if operation_id is not None:
                        operation_id = operation_id.strip()
                        if not operation_id or len(operation_id) > 200:
                            raise ValueError("X-Repogents-Operation-Id must be 1 to 200 characters")
                        added = service.application.add_repository(
                            repository.strip(),
                            None if target_branch is None else target_branch.strip(),
                            operation_id=operation_id,
                        )
                    else:
                        added = service.application.add_repository(
                            repository.strip(),
                            None if target_branch is None else target_branch.strip(),
                        )
                    response_headers = (
                        {"X-Repogents-Operation-Id": operation_id}
                        if operation_id is not None else None
                    )
                    self._send_json(HTTPStatus.CREATED, added, response_headers)
                except (ValueError, json.JSONDecodeError) as error:
                    self._error(HTTPStatus.BAD_REQUEST, error)
                except RepositoryLookupTimeoutError as error:
                    self._error(HTTPStatus.GATEWAY_TIMEOUT, error)
                except Exception as error:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, error)

            def do_DELETE(self) -> None:
                path = urlparse(self.path).path
                prefix = "/api/repositories/"
                if not path.startswith(prefix):
                    self._error(HTTPStatus.NOT_FOUND, "not found")
                    return
                try:
                    repository_id = int(path[len(prefix) :])
                    service.application.remove_repository(repository_id)
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST, error)
                except Exception as error:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, error)

        try:
            self._server = _LifecycleThreadingHTTPServer(
                (host, port), Handler, request_io_timeout=float(request_io_timeout)
            )
        except BaseException:
            # Application construction may allocate executors, but binding failure
            # must not acquire data ownership or run destructive startup recovery.
            self.application.close()
            raise
        try:
            acquire_ownership = getattr(
                self.application, "acquire_service_ownership", None
            )
            if acquire_ownership is not None:
                acquire_ownership()
        except BaseException:
            self._server.server_close()
            self.application.close()
            raise

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                self.application.poll_once()
            except Exception:
                pass
            self._stop.wait(self.poll_seconds)

    def serve_forever(self) -> None:
        self._poll_thread = threading.Thread(
            target=self._poll,
            name="repogents-poller",
            daemon=True,
        )
        self._poll_thread.start()
        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            # ``shutdown`` has stopped the serve loop, so close the listener and join
            # every already accepted request handler before stopping the poller. The
            # application and its data-directory ownership remain live throughout
            # both joins. This prevents a blocked mutation handler or poll callback
            # from committing after a replacement service acquires the same store.
            self._server.server_close()
            self._stop.set()
            if self._poll_thread is not None:
                self._poll_thread.join()
            self.application.close()

    def shutdown(self) -> None:
        # Only stop request acceptance here. The serve thread owns ordered teardown
        # of handlers, poller, application resources, and service ownership.
        self._server.shutdown()

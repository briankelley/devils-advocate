/* Devil's Advocate GUI — Client-side JS */

const dvad = {
    _sseSource: null,
    _revisionContent: '',
    _sortState: { col: null, asc: true },
    _picker: {
        targetField: null,
        multiSelect: false,
        dirMode: false,
        selected: [],
        currentDir: null,
    },
    // Stored paths per field (persisted across modal open/close)
    _selectedPaths: {
        input_files: [],
        reference_files: [],
        spec_file: [],
        project_dir: [],
    },

    // ── Vendor icon mapping ──────────────────────────────────────────
    VENDOR_ICONS: {
        'Anthropic': 'gem',
        'OpenAI': 'sparkles',
        'Google': 'globe',
        'xAI': 'zap',
        'DeepSeek': 'compass',
        'Moonshot': 'moon',
        'MiniMax': 'box',
    },

    // ── Table row click navigation ───────────────────────────────────
    init() {
        document.querySelectorAll('.clickable-row').forEach(row => {
            row.addEventListener('click', () => {
                window.location.href = row.dataset.href;
            });
        });

        // Project filter (Project is column 1, after Review ID)
        const filter = document.getElementById('project-filter');
        if (filter) {
            filter.addEventListener('input', () => {
                const val = filter.value.toLowerCase();
                document.querySelectorAll('#reviews-table tbody tr').forEach(tr => {
                    const project = tr.children[1]?.textContent?.toLowerCase() || '';
                    tr.style.display = project.includes(val) ? '' : 'none';
                });
            });
        }

        // Override buttons
        document.querySelectorAll('.override-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.overrideGroup(btn.dataset.group, btn.dataset.action);
            });
        });

        this.initSorting();
        this.initNewReviewForm();
        this.initTimeoutEditing();
        this.initMaxTokenEditing();
        this.initSettingsToggle();
        this.initEnvKeys();
    },

    // ── New Review Form (Dashboard) ─────────────────────────────────
    _formData: null,

    initNewReviewForm() {
        const form = document.getElementById('review-form');
        if (!form) return;

        const modeCards = document.querySelectorAll('.mode-card input[name="mode"]');
        const hiddenMode = document.getElementById('review-mode');

        // Sync mode cards to hidden field
        modeCards.forEach(radio => {
            radio.addEventListener('change', () => {
                hiddenMode.value = radio.value;
                this.updateModeUI();
            });
        });
        this.updateModeUI();

        // Form submit → pre-flight readiness check → show interstitial
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            // Build FormData manually with path-based fields
            const fd = new FormData();
            const mode = document.getElementById('review-mode')?.value || 'plan';
            fd.set('mode', mode);
            fd.set('project', document.getElementById('project')?.value || '');
            fd.set('max_cost', document.getElementById('max_cost')?.value || '');
            if (document.getElementById('dry_run')?.checked) fd.set('dry_run', 'on');
            fd.set('project_dir', document.getElementById('project_dir')?.value || '');

            // Path-based file inputs
            const inputHidden = document.getElementById('input_files_paths')?.value || '';
            if (inputHidden) fd.set('input_paths', inputHidden);
            const refHidden = document.getElementById('reference_files_paths')?.value || '';
            if (refHidden) fd.set('reference_paths', refHidden);
            const specHidden = document.getElementById('spec_file_paths')?.value || '';
            if (specHidden) fd.set('spec_path', specHidden);

            this._formData = fd;

            // Pre-flight readiness check
            try {
                const resp = await fetch('/api/config/readiness');
                const readiness = await resp.json();
                const modeState = readiness[mode];

                if (modeState && modeState.errors.length > 0) {
                    this.showValidationPopover(modeState.errors, 'error');
                    return;
                }

                if (modeState && modeState.warnings.length > 0) {
                    this.showValidationPopover(modeState.warnings, 'warn', () => {
                        this.showInterstitial();
                    });
                    return;
                }
            } catch (err) {
                // If readiness check fails, proceed anyway (server will catch at start_review)
            }

            this.showInterstitial();
        });
    },

    updateModeUI() {
        const mode = document.querySelector('input[name="mode"]:checked')?.value || 'plan';
        const browseBtn = document.getElementById('input-files-browse');
        const specRow = document.getElementById('spec-row');
        const projectDirRow = document.getElementById('project-dir-row');
        const referenceRow = document.getElementById('reference-files-row');
        const fileHint = document.getElementById('file-hint');
        const inputRow = document.getElementById('input-files-row');
        const inputLabel = document.getElementById('input-files-label');

        if (!browseBtn) return;

        if (mode === 'plan') {
            browseBtn.onclick = () => this.openFilePicker('input_files', false);
            inputLabel.textContent = 'Plan File';
            fileHint.textContent = 'The implementation plan to review';
            specRow.style.display = 'none';
            projectDirRow.style.display = 'none';
            referenceRow.style.display = '';
            inputRow.style.display = '';
        } else if (mode === 'code') {
            browseBtn.onclick = () => this.openFilePicker('input_files', false);
            inputLabel.textContent = 'Required Files';
            fileHint.textContent = 'Exactly one file required for code mode';
            specRow.style.display = '';
            projectDirRow.style.display = 'none';
            referenceRow.style.display = 'none';
            inputRow.style.display = '';
        } else if (mode === 'spec') {
            browseBtn.onclick = () => this.openFilePicker('input_files', true);
            inputLabel.textContent = 'Required Files';
            fileHint.textContent = 'Specification file(s) to enrich with suggestions';
            specRow.style.display = 'none';
            projectDirRow.style.display = 'none';
            referenceRow.style.display = 'none';
            inputRow.style.display = '';
        } else {
            // integration
            browseBtn.onclick = () => this.openFilePicker('input_files', true);
            inputLabel.textContent = 'Input Files';
            fileHint.textContent = 'Input files are optional for integration mode';
            specRow.style.display = '';
            projectDirRow.style.display = '';
            referenceRow.style.display = 'none';
            inputRow.style.display = '';
        }
    },

    buildCommand() {
        const mode = document.getElementById('review-mode')?.value || 'plan';
        const project = document.getElementById('project')?.value || '';
        const binary = (typeof dvadBinary !== 'undefined') ? dvadBinary : 'dvad';

        let parts = [binary, 'review', '--mode', mode, '--project', project];

        // Read from stored paths
        const inputPaths = this._selectedPaths.input_files || [];
        for (const s of inputPaths) {
            parts.push('--input', s.path);
        }

        const refPaths = this._selectedPaths.reference_files || [];
        for (const s of refPaths) {
            parts.push('--input', s.path);
        }

        const specPaths = this._selectedPaths.spec_file || [];
        if (specPaths.length > 0) {
            parts.push('--spec', specPaths[0].path);
        }

        const projectDir = document.getElementById('project_dir')?.value?.trim();
        if (projectDir) {
            parts.push('--project-dir', projectDir);
        }

        const maxCost = document.getElementById('max_cost')?.value?.trim();
        if (maxCost) {
            parts.push('--max-cost', maxCost);
        }

        if (document.getElementById('dry_run')?.checked) {
            parts.push('--dry-run');
        }

        return parts.join(' ');
    },

    showInterstitial() {
        const cmd = this.buildCommand();
        document.getElementById('command-preview-text').textContent = cmd;
        document.getElementById('interstitial').style.display = '';
        document.querySelector('.review-settings').style.display = 'none';
        document.querySelector('.mode-cards').style.display = 'none';
    },

    cancelInterstitial() {
        document.getElementById('interstitial').style.display = 'none';
        document.querySelector('.review-settings').style.display = '';
        document.querySelector('.mode-cards').style.display = '';
    },

    copyCommand() {
        const text = document.getElementById('command-preview-text')?.textContent || '';
        navigator.clipboard.writeText(text);
    },

    executeReview() {
        if (!this._formData) return;
        this._showConfirmDialog(
            'Start Review?',
            'This will consume API credits and cannot be undone mid-review.',
            'Start Review',
            () => this._doExecuteReview(),
            false,
        );
    },

    _doExecuteReview() {
        const runBtn = document.getElementById('run-review-btn');
        const errorDiv = document.getElementById('form-error');
        if (runBtn) { runBtn.disabled = true; runBtn.textContent = 'Starting...'; }
        if (errorDiv) errorDiv.style.display = 'none';

        const token = document.querySelector('meta[name="csrf-token"]')?.content || '';

        fetch('/api/review/start', {
            method: 'POST',
            headers: {'X-DVAD-Token': token},
            body: this._formData,
        })
        .then(r => r.json().then(data => ({ok: r.ok, data})))
        .then(({ok, data}) => {
            if (ok && data.review_id) {
                window.location.href = '/review/' + data.review_id;
            } else {
                if (errorDiv) {
                    errorDiv.textContent = data.detail || 'Failed to start review';
                    errorDiv.style.display = '';
                }
                if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Review'; }
                this.cancelInterstitial();
            }
        })
        .catch(err => {
            if (errorDiv) {
                errorDiv.textContent = 'Network error: ' + err.message;
                errorDiv.style.display = '';
            }
            if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Review'; }
            this.cancelInterstitial();
        });
    },

    // ── CSRF token ───────────────────────────────────────────────────
    getToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || '';
    },

    // ── Column sorting ───────────────────────────────────────────────
    initSorting() {
        const headers = document.querySelectorAll('.th-sortable');
        if (!headers.length) return;

        headers.forEach(th => {
            th.addEventListener('click', () => {
                const col = parseInt(th.dataset.col);
                const type = th.dataset.sortType || 'string';
                const asc = this._sortState.col === col ? !this._sortState.asc : true;
                this._sortState = { col, asc };

                // Update indicators
                headers.forEach(h => {
                    const ind = h.querySelector('.sort-indicator');
                    if (ind) ind.textContent = '';
                });
                const indicator = th.querySelector('.sort-indicator');
                if (indicator) indicator.textContent = asc ? ' \u25B2' : ' \u25BC';

                // Sort rows
                const tbody = document.querySelector('#reviews-table tbody');
                if (!tbody) return;
                const rows = Array.from(tbody.querySelectorAll('tr.clickable-row'));

                rows.sort((a, b) => {
                    const aVal = a.children[col]?.dataset.sortVal || '';
                    const bVal = b.children[col]?.dataset.sortVal || '';

                    let cmp;
                    if (type === 'numeric') {
                        cmp = (parseFloat(aVal) || 0) - (parseFloat(bVal) || 0);
                    } else {
                        cmp = aVal.localeCompare(bVal);
                    }
                    return asc ? cmp : -cmp;
                });

                rows.forEach(row => tbody.appendChild(row));
            });
        });
    },

    // ── Override group ───────────────────────────────────────────────
    async overrideGroup(groupId, resolution) {
        const card = document.getElementById('group-' + groupId);
        if (!card) return;

        const buttons = card.querySelectorAll('.override-btn');
        buttons.forEach(b => b.disabled = true);

        try {
            const reviewId = window.location.pathname.split('/review/')[1];
            const resp = await fetch(`/api/review/${reviewId}/override`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ group_id: groupId, resolution }),
            });

            const data = await resp.json();
            if (resp.ok) {
                card.classList.add('resolved');
                const actions = card.querySelector('.card-actions');
                if (actions) {
                    const label = {
                        'overridden': 'Accepted (Reviewer)',
                        'auto_dismissed': 'Accepted (Author)',
                        'escalated': 'Kept Open'
                    }[resolution] || resolution;
                    actions.innerHTML = `<span class="dim">Resolution: ${label}</span>`;
                }
                this._checkAllOverridesResolved();
            } else {
                alert(data.detail || 'Override failed');
                buttons.forEach(b => b.disabled = false);
            }
        } catch (err) {
            alert('Network error: ' + err.message);
            buttons.forEach(b => b.disabled = false);
        }
    },

    // ── Cancel Review ──────────────────────────────────────────────────
    cancelReview(reviewId) {
        this._showConfirmDialog(
            'Cancel Review?',
            'Any partial results will be lost. This cannot be undone.',
            'Cancel Review',
            () => this._doCancelReview(reviewId),
            true,
        );
    },

    async _doCancelReview(reviewId) {
        const btn = document.getElementById('cancel-review-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Cancelling...'; }
        try {
            const resp = await fetch(`/api/review/${reviewId}/cancel`, {
                method: 'POST',
                headers: { 'X-DVAD-Token': this.getToken() },
            });
            if (!resp.ok) {
                const data = await resp.json();
                alert(data.detail || 'Cancel failed');
                if (btn) { btn.disabled = false; btn.textContent = 'Cancel Review'; }
            }
        } catch (err) {
            alert('Network error: ' + err.message);
            if (btn) { btn.disabled = false; btn.textContent = 'Cancel Review'; }
        }
    },

    // ── SSE Progress ─────────────────────────────────────────────────
    connectSSE(reviewId) {
        const logOutput = document.getElementById('log-output');
        const source = new EventSource(`/api/review/${reviewId}/progress`);
        this._sseSource = source;

        const seenPhases = new Set();

        source.onmessage = (e) => {
            try {
                const ev = JSON.parse(e.data);

                // Handle metadata event (role→model mapping for cost table)
                if (ev.type === 'metadata') {
                    this._handleMetadata(ev.detail);
                    return;
                }

                // Handle cost event (update cost table, suppress from log)
                if (ev.type === 'cost') {
                    this._handleCostUpdate(ev.detail);
                    return;
                }

                // Append to log (all other events with a message)
                if (ev.message && logOutput) {
                    const line = document.createElement('div');
                    const ts = ev.timestamp ? `[${ev.timestamp}] ` : '';
                    line.textContent = ts + ev.message;
                    logOutput.appendChild(line);
                    const scrollParent = logOutput.parentElement;
                    if (scrollParent) scrollParent.scrollTop = scrollParent.scrollHeight;
                }

                // Detect spec mode from the initial review_start event
                if (ev.phase === 'review_start' && ev.message && ev.message.includes('spec review')) {
                    this._applySpecMode();
                }

                // Update phase dots
                if (ev.phase) {
                    this._updatePhase(ev.phase, seenPhases);
                }

                // Terminal events
                if (ev.type === 'complete') {
                    source.close();
                    setTimeout(() => window.location.reload(), 500);
                } else if (ev.type === 'error') {
                    source.close();
                    const cancelBtn = document.getElementById('cancel-review-btn');
                    if (cancelBtn) cancelBtn.style.display = 'none';
                    if (logOutput) {
                        const errLine = document.createElement('div');
                        errLine.style.color = '#ff4757';
                        errLine.textContent = 'ERROR: ' + (ev.message || 'Review failed');
                        logOutput.appendChild(errLine);
                    }
                }
            } catch (parseErr) {
                // Ignore parse errors (e.g., keepalive pings)
            }
        };

        source.onerror = () => {
            source.close();
            setTimeout(() => window.location.reload(), 3000);
        };
    },

    _handleMetadata(detail) {
        if (!detail) return;

        // Set mode label
        const modeLabel = document.getElementById('review-mode-label');
        if (modeLabel && detail.mode) {
            modeLabel.textContent = detail.mode + ' review';
        }

        // Build cost table from roles
        const table = document.getElementById('live-cost-table');
        if (!table || !detail.roles) return;

        const roles = detail.roles;
        let html = '';
        for (const [role, model] of Object.entries(roles)) {
            const label = role.replace(/_/g, ' ');
            html += `<div class="cost-row">` +
                `<span class="cost-role">${label}:</span>` +
                `<span class="cost-model">${model}</span>` +
                `<span class="cost-value" id="cost-${role}">$0.000000</span>` +
                `</div>`;
        }
        html += `<div class="cost-row cost-divider"></div>`;
        html += `<div class="cost-row">` +
            `<span class="cost-role"></span>` +
            `<span class="cost-model"></span>` +
            `<span class="cost-value cost-total-value" id="cost-total">$0.000000</span>` +
            `</div>`;
        table.innerHTML = html;

        // Spec mode: hide adversarial phases, show spec-only steps
        if (detail.mode === 'spec') {
            this._applySpecMode();
        }
    },

    _roleCosts: {},

    _handleCostUpdate(detail) {
        if (!detail) return;
        const role = detail.role;
        const callCost = parseFloat(detail.cost) || 0;

        // Accumulate per-role cost
        this._roleCosts[role] = (this._roleCosts[role] || 0) + callCost;

        const costEl = document.getElementById('cost-' + role);
        if (costEl) {
            costEl.textContent = '$' + this._roleCosts[role].toFixed(6);
        }
        const totalEl = document.getElementById('cost-total');
        if (totalEl) {
            totalEl.textContent = '$' + parseFloat(detail.total).toFixed(6);
        }
    },

    _applySpecMode() {
        if (this._specMode) return;
        this._specMode = true;
        document.querySelectorAll('.phase-adversarial').forEach(el => {
            el.style.display = 'none';
        });
        document.querySelectorAll('.bc-spec-only').forEach(el => {
            el.classList.remove('bc-spec-only');
        });
        const reviewersLabel = document.getElementById('reviewers-phase-label');
        if (reviewersLabel) reviewersLabel.textContent = 'REVIEWERS';
        const finalLabel = document.getElementById('final-phase-label');
        if (finalLabel) finalLabel.textContent = 'SUGGESTIONS';
    },

    _updatePhase(phase, seenPhases) {
        const phaseMap = {
            'review_start': 'dot-reviewers',
            'round1_calling': 'dot-reviewers',
            'round1_responded': 'dot-reviewers',
            'normalization': 'dot-reviewers',
            'dedup_calling': 'dot-dedup',
            'dedup_responded': 'dot-dedup',
            'round1_author': 'dot-author',
            'round2_skip': 'dot-rebuttals',
            'round2_skip_reviewer': 'dot-rebuttals',
            'round2_skip_context': 'dot-rebuttals',
            'round2_rebuttal_failed': 'dot-rebuttals',
            'round2_author_failed': 'dot-rebuttals',
            'governance_catastrophic': 'dot-governance',
            'governance_complete': 'dot-governance',
            'cost_warning': null,
            'cost_exceeded': null,
            'revision_calling': 'dot-revision',
            'revision_responded': 'dot-revision',
            'revision_skip': 'dot-revision',
            'revision_skip_context': 'dot-revision',
            'revision_extraction_failed': 'dot-revision',
            'revision_failed': 'dot-revision',
        };

        const dotId = phaseMap[phase];
        if (!dotId) return;

        const fullOrder = ['dot-reviewers', 'dot-dedup', 'dot-author', 'dot-rebuttals', 'dot-governance', 'dot-revision'];
        const dotOrder = fullOrder.filter(id => document.getElementById(id));
        const currentIdx = dotOrder.indexOf(dotId);

        for (let i = 0; i < dotOrder.length; i++) {
            const dot = document.getElementById(dotOrder[i]);
            if (!dot) continue;
            if (i < currentIdx) {
                dot.innerHTML = '&#9679;';
                dot.className = 'phase-dot done';
            } else if (i === currentIdx) {
                dot.innerHTML = '&#9679;';
                dot.className = 'phase-dot active';
            }
        }
    },

    _checkAllOverridesResolved() {
        const cards = document.querySelectorAll('.group-card.card-escalated');
        if (!cards.length) return;
        const allResolved = Array.from(cards).every(c => c.classList.contains('resolved'));
        if (allResolved) {
            // Mark overrides pipeline step as done
            const overrideStep = document.getElementById('pipe-overrides');
            if (overrideStep) {
                overrideStep.className = 'pipeline-step done';
            }
            // Show banner
            const banner = document.getElementById('overrides-banner');
            if (banner) banner.classList.add('visible');
            // Highlight revision button
            const reviseBtn = document.getElementById('revise-btn');
            if (reviseBtn) {
                reviseBtn.classList.add('btn-accent');
                reviseBtn.style.animation = 'step-pulse 2s ease-in-out 3';
            }
        }
    },

    // ── Revision ─────────────────────────────────────────────────────
    async startRevision(reviewId) {
        const btn = document.getElementById('revise-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Generating...';
        }

        try {
            const resp = await fetch(`/api/review/${reviewId}/revise`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
            });

            const data = await resp.json();
            if (resp.ok && data.content) {
                this._revisionContent = data.content;
                const output = document.getElementById('revision-output');
                const content = document.getElementById('revision-content');
                const cost = document.getElementById('revision-cost');
                if (output && content) {
                    content.textContent = data.content;
                    output.style.display = '';
                }
                if (cost) {
                    cost.textContent = `Cost: $${(data.cost || 0).toFixed(6)}`;
                }

                // Show patch status for code mode
                if (data.patch_applied !== undefined) {
                    this._showPatchStatus(data);
                }

                // Hide generate button, update or add download link
                if (btn) btn.style.display = 'none';
                const footer = document.querySelector('.footer-actions');
                const existing = footer && footer.querySelector('.download-revised-link');
                if (existing) {
                    existing.textContent = 'Download Revised';
                    existing.className = 'btn btn-green download-revised-link';
                } else if (footer) {
                    const link = document.createElement('a');
                    link.href = `/api/review/${reviewId}/revised`;
                    link.className = 'btn btn-green download-revised-link';
                    link.textContent = 'Download Revised';
                    footer.insertBefore(link, footer.firstChild);
                }
                // Mark revision pipeline step as done
                const revisionStep = document.getElementById('pipe-revision');
                if (revisionStep) {
                    revisionStep.className = 'pipeline-step done';
                }
            } else {
                alert(data.detail || data.message || 'Revision failed');
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Generate Revision';
                }
            }
        } catch (err) {
            alert('Network error: ' + err.message);
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Generate Revision';
            }
        }
    },

    async showLog(reviewId) {
        const container = document.getElementById('completed-log');
        const output = document.getElementById('completed-log-output');
        const btn = document.getElementById('show-log-btn');
        if (!container || !output || !btn) return;

        if (container.style.display !== 'none') {
            container.style.display = 'none';
            btn.textContent = 'Show Log';
            return;
        }

        btn.textContent = 'Loading...';
        try {
            const resp = await fetch(`/api/review/${reviewId}/log`);
            if (!resp.ok) {
                output.textContent = 'Log not available.';
            } else {
                output.textContent = await resp.text();
            }
        } catch (err) {
            output.textContent = 'Failed to load log: ' + err.message;
        }
        container.style.display = '';
        btn.textContent = 'Hide Log';
    },

    copyRevision() {
        if (this._revisionContent) {
            navigator.clipboard.writeText(this._revisionContent);
        }
    },

    _showPatchStatus(data) {
        const statusEl = document.getElementById('patch-status');
        const regenBtn = document.getElementById('regen-full-btn');
        if (!statusEl) return;

        if (data.patch_applied) {
            statusEl.style.display = '';
            statusEl.innerHTML = '<span style="color:var(--green)">Patch applied successfully. Full revised file saved.</span>';
        } else if (data.patch_error) {
            statusEl.style.display = '';
            statusEl.innerHTML = '<span style="color:var(--yellow)">Patch could not be applied: ' +
                data.patch_error.substring(0, 200) + '</span>';
            if (regenBtn) regenBtn.style.display = '';
        }
    },

    async regenFullFile(reviewId) {
        const btn = document.getElementById('regen-full-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Regenerating...'; }

        try {
            const resp = await fetch(`/api/review/${reviewId}/revise-full`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
            });
            const data = await resp.json();
            if (resp.ok && data.content) {
                this._revisionContent = data.content;
                const content = document.getElementById('revision-content');
                const cost = document.getElementById('revision-cost');
                const statusEl = document.getElementById('patch-status');
                if (content) content.textContent = data.content;
                if (cost) cost.textContent = `Cost: $${(data.cost || 0).toFixed(6)}`;
                if (statusEl) {
                    statusEl.innerHTML = '<span style="color:var(--green)">Full file regenerated successfully.</span>';
                }
                if (btn) btn.style.display = 'none';
            } else {
                alert(data.detail || data.message || 'Regeneration failed');
                if (btn) { btn.disabled = false; btn.textContent = 'Regenerate as Full File'; }
            }
        } catch (err) {
            alert('Network error: ' + err.message);
            if (btn) { btn.disabled = false; btn.textContent = 'Regenerate as Full File'; }
        }
    },

    // ── Config tabs ──────────────────────────────────────────────────
    switchTab(tab) {
        const onStructured = document.querySelector('.tab-btn.active')?.dataset.tab === 'structured';
        if (onStructured && tab !== 'structured' && this._pendingState && this._originalState &&
            JSON.stringify(this._pendingState) !== JSON.stringify(this._originalState)) {
            this._showConfirmDialog(
                'Unsaved Changes',
                'You have unsaved role changes. Switching tabs will discard them.',
                'Discard',
                () => {
                    this._pendingState = JSON.parse(JSON.stringify(this._originalState));
                    this._renderRoles();
                    this._doSwitchTab(tab);
                },
                true
            );
            return;
        }
        this._doSwitchTab(tab);
    },

    _doSwitchTab(tab) {
        document.querySelectorAll('.tab-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.tab === tab);
        });
        document.querySelectorAll('.tab-content').forEach(c => {
            c.style.display = c.id === 'tab-' + tab ? '' : 'none';
        });
    },

    async validateYaml() {
        const yaml = document.getElementById('yaml-editor')?.value || '';
        const result = document.getElementById('yaml-result');

        try {
            const resp = await fetch('/api/config/validate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ yaml }),
            });
            const data = await resp.json();
            if (result) {
                if (data.valid) {
                    result.innerHTML = '<span style="color:#2ed573">Configuration is valid.</span>';
                } else {
                    result.innerHTML = (data.issues || []).map(([level, msg]) =>
                        `<div class="issue issue-${level}">${level.toUpperCase()}: ${msg}</div>`
                    ).join('');
                }

                // Show warnings even when valid
                if (data.valid && data.issues && data.issues.length > 0) {
                    result.innerHTML += (data.issues).map(([level, msg]) =>
                        `<div class="issue issue-${level}">${level.toUpperCase()}: ${msg}</div>`
                    ).join('');
                }
            }
        } catch (err) {
            if (result) result.textContent = 'Error: ' + err.message;
        }
    },

    saveYaml() {
        this._showConfirmDialog(
            'Save Configuration?',
            'This will overwrite the current models.yaml file.',
            'Save Config',
            () => this._doSaveYaml(),
            false,
        );
    },

    async _doSaveYaml() {
        const yaml = document.getElementById('yaml-editor')?.value || '';
        const result = document.getElementById('yaml-result');

        try {
            const resp = await fetch('/api/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ yaml }),
            });
            const data = await resp.json();
            if (result) {
                if (resp.ok) {
                    result.innerHTML = `<span style="color:#2ed573">Saved to ${data.path}</span>`;
                } else {
                    result.innerHTML = `<span style="color:#ff4757">${data.detail}</span>`;
                }
            }
        } catch (err) {
            if (result) result.textContent = 'Error: ' + err.message;
        }
    },

    // ── Inline Number Editing (shared by timeout + max tokens) ────────
    _initInlineEditor(selector, { min, max, endpoint, bodyKey, placeholder, emptyValue }) {
        document.querySelectorAll(selector).forEach(span => {
            span.addEventListener('click', () => {
                if (span.querySelector('input')) return;
                const currentVal = span.textContent.trim();
                const modelName = span.dataset.model;

                const input = document.createElement('input');
                input.type = 'number';
                input.min = String(min);
                input.max = String(max);
                input.value = (emptyValue && currentVal === emptyValue) ? '' : currentVal;
                input.className = 'timeout-input';
                if (placeholder) input.placeholder = placeholder;

                span.textContent = '';
                span.appendChild(input);
                input.focus();
                input.select();

                const commit = async () => {
                    const rawVal = input.value.trim();

                    // Handle empty → null (for clearable fields like max_out_configured)
                    if (emptyValue && rawVal === '') {
                        span.textContent = emptyValue;
                        try {
                            await fetch(endpoint, {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-DVAD-Token': this.getToken(),
                                },
                                body: JSON.stringify({ model_name: modelName, [bodyKey]: null, clear: true }),
                            });
                        } catch (err) { /* ignore */ }
                        return;
                    }

                    const newVal = parseInt(rawVal);
                    if (isNaN(newVal) || newVal < min || newVal > max) {
                        span.textContent = currentVal;
                        return;
                    }

                    span.textContent = newVal;

                    try {
                        const resp = await fetch(endpoint, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-DVAD-Token': this.getToken(),
                            },
                            body: JSON.stringify({ model_name: modelName, [bodyKey]: newVal }),
                        });
                        if (!resp.ok) {
                            const data = await resp.json();
                            alert(data.detail || `Failed to update ${bodyKey}`);
                            span.textContent = currentVal;
                        }
                    } catch (err) {
                        alert('Network error: ' + err.message);
                        span.textContent = currentVal;
                    }
                };

                input.addEventListener('blur', commit);
                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
                    else if (e.key === 'Escape') { span.textContent = currentVal; }
                });
            });
        });
    },

    initTimeoutEditing() {
        this._initInlineEditor('.editable-timeout', {
            min: 10, max: 7200,
            endpoint: '/api/config/model-timeout',
            bodyKey: 'timeout',
        });
    },

    initMaxTokenEditing() {
        this._initInlineEditor('.editable-max-tokens', {
            min: 1, max: 1000000,
            endpoint: '/api/config/model-max-tokens',
            bodyKey: 'max_out_configured',
            placeholder: 'unset',
            emptyValue: 'unset',
        });
    },

    // ── Settings Toggle ──────────────────────────────────────────────
    initSettingsToggle() {
        document.querySelectorAll('.settings-toggle').forEach(el => {
            el.addEventListener('click', async (e) => {
                e.stopPropagation();
                const key = el.dataset.key;
                const currentlyOn = el.classList.contains('settings-on');
                const newValue = !currentlyOn;

                const resp = await fetch('/api/config/settings-toggle', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-DVAD-Token': dvad.getToken(),
                    },
                    body: JSON.stringify({ key, value: newValue }),
                });

                if (resp.ok) {
                    el.classList.toggle('settings-on', newValue);
                    el.textContent = newValue ? 'enabled' : 'disabled';
                }
            });
        });
    },

    // ── API Key Management ───────────────────────────────────────────

    initEnvKeys() {
        const section = document.getElementById('env-keys-section');
        if (!section) return;

        fetch('/api/config/env')
            .then(resp => {
                if (!resp.ok) throw new Error(`HTTP error ${resp.status}`);
                return resp.json();
            })
            .then(data => {
                const list = document.getElementById('env-keys-list');
                if (!list) return;

                if (data.status === 'config_dir_unknown') {
                    list.innerHTML = '<p class="dim">Config directory unknown.</p>';
                    return;
                }
                if (!data.env_vars || data.env_vars.length === 0) {
                    list.innerHTML = '<p class="dim">No API key variables configured in models.</p>';
                    return;
                }

                list.innerHTML = '';
                data.env_vars.forEach(ev => {
                    if (!/^[A-Z_][A-Z0-9_]*$/.test(ev.env_name)) return;
                    this._renderEnvKeyRow(list, ev);
                });
            })
            .catch(err => {
                const list = document.getElementById('env-keys-list');
                if (list) list.innerHTML = `<p class="dim">Failed to load: ${err.message}</p>`;
            });
    },

    _renderEnvKeyRow(container, ev) {
        const row = document.createElement('div');
        row.className = 'env-key-row';
        row.id = `env-row-${ev.env_name}`;

        const label = document.createElement('span');
        label.className = 'env-key-label';
        label.textContent = ev.env_name;
        row.appendChild(label);

        if (ev.in_env_file && ev.abbreviated) {
            // Key is present: show "present" badge + abbreviated key + Clear button
            const badge = document.createElement('span');
            badge.className = 'key-status key-set';
            badge.textContent = 'present';
            row.appendChild(badge);

            const abbr = document.createElement('span');
            abbr.className = 'env-key-abbreviated mono';
            abbr.textContent = ev.abbreviated;
            row.appendChild(abbr);

            const clearBtn = document.createElement('button');
            clearBtn.className = 'btn btn-sm btn-cancel';
            clearBtn.textContent = 'Clear';
            clearBtn.addEventListener('click', () => this._clearEnvVar(ev.env_name));
            row.appendChild(clearBtn);
        } else {
            // Key not present: show input + Save button
            const input = document.createElement('input');
            input.type = 'password';
            input.className = 'env-key-input';
            input.dataset.envName = ev.env_name;
            input.placeholder = 'paste key here';
            input.autocomplete = 'off';
            row.appendChild(input);

            const saveBtn = document.createElement('button');
            saveBtn.className = 'btn btn-sm btn-accent';
            saveBtn.textContent = 'Save';
            saveBtn.addEventListener('click', () => this._saveEnvVar(ev.env_name, input.value));
            row.appendChild(saveBtn);

            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); this._saveEnvVar(ev.env_name, input.value); }
            });
            input.addEventListener('dblclick', () => {
                input.type = input.type === 'password' ? 'text' : 'password';
            });
        }

        container.appendChild(row);
    },

    async _saveEnvVar(name, value) {
        if (!value || !value.trim()) return;

        try {
            const resp = await fetch(`/api/config/env/${encodeURIComponent(name)}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ value }),
            });
            if (resp.ok) {
                // Re-render this row as "present"
                this._refreshEnvKeys();
            } else {
                const data = await resp.json();
                alert(data.detail || 'Save failed');
            }
        } catch (err) {
            alert('Network error: ' + err.message);
        }
    },

    _clearEnvVar(name) {
        this._showConfirmDialog(
            'Delete API Key?',
            `Delete ${name}? This removes it from .env and cannot be undone.`,
            'Delete Key',
            () => this._doClearEnvVar(name),
            true,
        );
    },

    async _doClearEnvVar(name) {
        try {
            const resp = await fetch(`/api/config/env/${encodeURIComponent(name)}`, {
                method: 'DELETE',
                headers: {
                    'X-DVAD-Token': this.getToken(),
                    'X-Confirm-Destructive': 'true',
                },
            });
            if (resp.ok) {
                this._refreshEnvKeys();
            } else {
                const data = await resp.json();
                alert(data.detail || 'Clear failed');
            }
        } catch (err) {
            alert('Network error: ' + err.message);
        }
    },

    _refreshEnvKeys() {
        // Re-fetch and re-render all env key rows
        const list = document.getElementById('env-keys-list');
        if (!list) return;
        fetch('/api/config/env')
            .then(r => r.json())
            .then(data => {
                list.innerHTML = '';
                (data.env_vars || []).forEach(ev => {
                    if (!/^[A-Z_][A-Z0-9_]*$/.test(ev.env_name)) return;
                    this._renderEnvKeyRow(list, ev);
                });
            });
    },
    // ── File Picker ─────────────────────────────────────────────────

    openFilePicker(targetField, multiSelect, dirMode = false) {
        this._picker.targetField = targetField;
        this._picker.multiSelect = multiSelect;
        this._picker.dirMode = dirMode;
        // Copy existing selections for this field into picker state
        this._picker.selected = [...(this._selectedPaths[targetField] || [])];

        const title = dirMode ? 'Select Directory' : (multiSelect ? 'Select Files' : 'Select File');
        const titleEl = document.getElementById('picker-title');
        if (titleEl) titleEl.textContent = title;

        document.getElementById('file-picker-modal').classList.add('visible');
        this._fetchDir(this._picker.currentDir || '~');
    },

    _fetchDir(dir) {
        fetch('/api/fs/ls?dir=' + encodeURIComponent(dir))
            .then(r => r.json())
            .then(data => {
                if (data.detail) {
                    // Error from API
                    const list = document.getElementById('picker-file-list');
                    if (list) list.innerHTML = `<p class="dim">${data.detail}</p>`;
                    return;
                }
                this._picker.currentDir = data.current_dir;
                this._renderPicker(data);
            })
            .catch(err => {
                const list = document.getElementById('picker-file-list');
                if (list) list.innerHTML = `<p class="dim">Error: ${err.message}</p>`;
            });
    },

    _renderPicker(data) {
        // Breadcrumb
        const bc = document.getElementById('picker-breadcrumb');
        if (bc) {
            const parts = data.current_dir.split('/').filter(Boolean);
            let html = '<span class="breadcrumb-seg" onclick="dvad._fetchDir(\'/\')">/</span>';
            let cumPath = '';
            for (const part of parts) {
                cumPath += '/' + part;
                const p = cumPath;
                html += `<span class="breadcrumb-sep">/</span><span class="breadcrumb-seg" onclick="dvad._fetchDir('${p.replace(/'/g, "\\'")}')">${part}</span>`;
            }
            bc.innerHTML = html;
        }

        // File list
        const list = document.getElementById('picker-file-list');
        if (!list) return;

        let html = '';

        // Parent directory entry
        if (data.parent_dir) {
            html += `<div class="file-entry is-dir" ondblclick="dvad._fetchDir('${data.parent_dir.replace(/'/g, "\\'")}')" onclick="dvad._fetchDir('${data.parent_dir.replace(/'/g, "\\'")}')">
                <i data-lucide="corner-left-up" class="file-icon"></i>
                <span class="file-name">..</span>
            </div>`;
        }

        if (data.error) {
            html += `<p class="dim" style="padding:var(--pad-md)">${data.error}</p>`;
        }

        for (const entry of (data.entries || [])) {
            const isSelected = this._picker.selected.some(s => s.path === entry.path);
            const selClass = isSelected ? ' selected' : '';
            const dirClass = entry.is_dir ? ' is-dir' : '';
            const icon = entry.is_dir ? 'folder' : 'file-text';
            const size = entry.is_dir ? '' : this._formatSize(entry.size);
            const escapedPath = entry.path.replace(/'/g, "\\'");
            const entryJson = JSON.stringify(entry).replace(/'/g, "&#39;").replace(/"/g, '&quot;');

            html += `<div class="file-entry${dirClass}${selClass}" data-path="${entry.path}" onclick="dvad._onEntryClick(${entryJson})" ondblclick="dvad._onEntryDblClick(${entryJson})">
                <i data-lucide="${icon}" class="file-icon"></i>
                <span class="file-name">${entry.name}</span>
                <span class="file-size">${size}</span>
            </div>`;
        }

        if (!data.entries || data.entries.length === 0) {
            if (!data.error) html += '<p class="dim" style="padding:var(--pad-md)">Empty directory</p>';
        }

        list.innerHTML = html;
        if (typeof lucide !== 'undefined') lucide.createIcons();
        this._updatePickerSelection();
    },

    _formatSize(bytes) {
        if (bytes == null) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    },

    _onEntryClick(entry) {
        if (entry.is_dir && !this._picker.dirMode) {
            // Navigate into directory
            this._fetchDir(entry.path);
            return;
        }

        if (entry.is_dir && this._picker.dirMode) {
            // In dir mode, single click selects the directory
            this._picker.selected = [{ path: entry.path, name: entry.name }];
            this._updatePickerSelection();
            this._highlightEntries();
            return;
        }

        // File: toggle or replace selection
        const idx = this._picker.selected.findIndex(s => s.path === entry.path);
        if (idx >= 0) {
            this._picker.selected.splice(idx, 1);
        } else {
            if (this._picker.multiSelect) {
                this._picker.selected.push({ path: entry.path, name: entry.name });
            } else {
                this._picker.selected = [{ path: entry.path, name: entry.name }];
            }
        }
        this._updatePickerSelection();
        this._highlightEntries();
    },

    _onEntryDblClick(entry) {
        if (entry.is_dir) {
            // Always navigate on double-click (even in dirMode)
            this._fetchDir(entry.path);
        }
    },

    _highlightEntries() {
        document.querySelectorAll('#picker-file-list .file-entry').forEach(el => {
            const path = el.dataset.path;
            const isSelected = this._picker.selected.some(s => s.path === path);
            el.classList.toggle('selected', isSelected);
        });
    },

    _updatePickerSelection() {
        const container = document.getElementById('picker-selected');
        if (!container) return;

        if (this._picker.selected.length === 0) {
            container.innerHTML = '<span class="dim">No files selected</span>';
        } else {
            container.innerHTML = this._picker.selected.map(s =>
                `<span class="file-chip">${s.name}<button class="chip-remove" onclick="event.stopPropagation(); dvad._removePickerSelection('${s.path.replace(/'/g, "\\'")}')">&times;</button></span>`
            ).join('');
        }

        const confirmBtn = document.getElementById('picker-confirm-btn');
        if (confirmBtn) confirmBtn.disabled = this._picker.selected.length === 0;
    },

    _removePickerSelection(path) {
        this._picker.selected = this._picker.selected.filter(s => s.path !== path);
        this._updatePickerSelection();
        this._highlightEntries();
    },

    confirmFilePicker() {
        const field = this._picker.targetField;
        this._selectedPaths[field] = [...this._picker.selected];

        if (field === 'project_dir') {
            // Write to the text input directly
            const input = document.getElementById('project_dir');
            if (input && this._picker.selected.length > 0) {
                input.value = this._picker.selected[0].path;
            }
        } else {
            // Write paths JSON to hidden input
            const hidden = document.getElementById(field + '_paths');
            if (hidden) {
                if (field === 'spec_file') {
                    // Single path string
                    hidden.value = this._picker.selected.length > 0 ? this._picker.selected[0].path : '';
                } else {
                    hidden.value = JSON.stringify(this._picker.selected.map(s => s.path));
                }
            }

            // Render chips in display div
            const display = document.getElementById(field + '_display');
            if (display) {
                display.innerHTML = this._picker.selected.map(s =>
                    `<span class="file-chip">${s.name}<button class="chip-remove" onclick="event.stopPropagation(); dvad._removeSelectedFile('${field}', '${s.path.replace(/'/g, "\\'")}')">&times;</button></span>`
                ).join('');
            }
        }

        this.closeFilePicker();
    },

    closeFilePicker() {
        document.getElementById('file-picker-modal').classList.remove('visible');
    },

    // ── Validation Popover ────────────────────────────────────────────
    showValidationPopover(messages, level, onConfirm) {
        const overlay = document.getElementById('validation-modal');
        if (!overlay) return;
        const list = document.getElementById('validation-messages');
        const confirmBtn = document.getElementById('validation-confirm-btn');
        const configBtn = document.getElementById('validation-config-btn');

        list.innerHTML = messages.map(msg =>
            `<div class="validation-msg validation-${level}">${msg}</div>`
        ).join('');

        if (level === 'error') {
            confirmBtn.style.display = 'none';
            configBtn.style.display = '';
        } else {
            confirmBtn.style.display = '';
            configBtn.style.display = 'none';
            confirmBtn.onclick = () => {
                overlay.classList.remove('visible');
                if (onConfirm) onConfirm();
            };
        }

        overlay.classList.add('visible');
    },

    closeValidationPopover() {
        const overlay = document.getElementById('validation-modal');
        if (overlay) overlay.classList.remove('visible');
    },

    // ── Confirmation Dialog ─────────────────────────────────────────
    _showConfirmDialog(title, message, confirmLabel, onConfirm, destructive = false) {
        document.getElementById('confirm-title').textContent = title;
        document.getElementById('confirm-message').textContent = message;
        const btn = document.getElementById('confirm-action-btn');
        btn.textContent = confirmLabel;
        btn.className = destructive ? 'btn btn-cancel' : 'btn btn-accent';
        btn.onclick = () => {
            dvad.closeConfirmDialog();
            onConfirm();
        };
        document.getElementById('confirm-modal').classList.add('visible');
    },

    closeConfirmDialog() {
        document.getElementById('confirm-modal').classList.remove('visible');
    },

    _removeSelectedFile(targetField, path) {
        this._selectedPaths[targetField] = this._selectedPaths[targetField].filter(s => s.path !== path);
        const remaining = this._selectedPaths[targetField];

        // Update hidden input
        const hidden = document.getElementById(targetField + '_paths');
        if (hidden) {
            if (targetField === 'spec_file') {
                hidden.value = remaining.length > 0 ? remaining[0].path : '';
            } else {
                hidden.value = remaining.length > 0 ? JSON.stringify(remaining.map(s => s.path)) : '';
            }
        }

        // Update display chips
        const display = document.getElementById(targetField + '_display');
        if (display) {
            display.innerHTML = remaining.map(s =>
                `<span class="file-chip">${s.name}<button class="chip-remove" onclick="event.stopPropagation(); dvad._removeSelectedFile('${targetField}', '${s.path.replace(/'/g, "\\'")}')">&times;</button></span>`
            ).join('');
        }
    },

    // ── Role/CoT Interactivity (Config Page) ─────────────────────────

    _pendingState: null,
    _originalState: null,

    // Role key to data-role attribute mapping (used in models table icons)
    _roleKeyToDataRole: {
        author: 'author',
        reviewer1: 'reviewer',
        reviewer2: 'reviewer',
        dedup: 'deduplication',
        normalization: 'normalization',
        revision: 'revision',
        integration: 'integration_reviewer',
    },

    // Singular roles (not reviewer)
    _singularRoles: ['author', 'dedup', 'normalization', 'revision', 'integration'],

    initRoleInteractivity() {
        if (typeof initialRoleState === 'undefined') return;

        this._pendingState = JSON.parse(JSON.stringify(initialRoleState));
        this._originalState = JSON.parse(JSON.stringify(initialRoleState));

        // Attach click handlers to role icons in model cards
        document.querySelectorAll('.model-role-icons .role-icon').forEach(icon => {
            icon.addEventListener('click', (e) => {
                e.stopPropagation();
                const model = icon.dataset.model;
                const dataRole = icon.dataset.role;
                this._handleRoleClick(model, dataRole);
            });
        });

        // Attach click handlers to thinking icons in model cards
        document.querySelectorAll('.model-role-icons .thinking-icon').forEach(icon => {
            icon.addEventListener('click', (e) => {
                e.stopPropagation();
                const model = icon.dataset.model;
                this._handleThinkingClick(model);
            });
        });

        this._renderRoles();
    },

    _handleRoleClick(model, dataRole) {
        const roles = this._pendingState.roles;

        if (dataRole === 'reviewer') {
            // Reviewer logic: ceiling = 2
            if (roles.reviewer1 === model) {
                // Unassign reviewer1, compact
                roles.reviewer1 = roles.reviewer2;
                roles.reviewer2 = null;
                this._clearThinkingIfOrphaned(model);
            } else if (roles.reviewer2 === model) {
                // Unassign reviewer2
                roles.reviewer2 = null;
                this._clearThinkingIfOrphaned(model);
            } else if (!roles.reviewer1) {
                roles.reviewer1 = model;
                this._pendingState.thinking[model] = this._pendingState.thinking[model] || false;
            } else if (!roles.reviewer2) {
                roles.reviewer2 = model;
                this._pendingState.thinking[model] = this._pendingState.thinking[model] || false;
            }
            // else: both slots full, no-op
        } else {
            // Singular role: map data-role to role key
            const roleKey = this._dataRoleToKey(dataRole);
            if (!roleKey) return;

            if (roles[roleKey] === model) {
                // Unassign
                roles[roleKey] = null;
                this._clearThinkingIfOrphaned(model);
            } else {
                // Assign (replaces any existing model)
                const oldModel = roles[roleKey];
                roles[roleKey] = model;
                this._pendingState.thinking[model] = this._pendingState.thinking[model] || false;
                if (oldModel) this._clearThinkingIfOrphaned(oldModel);
            }
        }

        this._renderRoles();
    },

    _dataRoleToKey(dataRole) {
        const map = {
            author: 'author',
            deduplication: 'dedup',
            normalization: 'normalization',
            revision: 'revision',
            integration_reviewer: 'integration',
        };
        return map[dataRole] || null;
    },

    _handleThinkingClick(model) {
        // No-op if model has no role assignments
        if (!this._modelHasRole(model)) return;
        this._pendingState.thinking[model] = !this._pendingState.thinking[model];
        this._renderRoles();
    },

    _modelHasRole(model) {
        const r = this._pendingState.roles;
        return r.author === model || r.reviewer1 === model || r.reviewer2 === model ||
            r.dedup === model || r.normalization === model || r.revision === model ||
            r.integration === model;
    },

    _clearThinkingIfOrphaned(model) {
        if (!this._modelHasRole(model)) {
            this._pendingState.thinking[model] = false;
        }
    },

    _renderRoles() {
        const roles = this._pendingState.roles;
        const thinking = this._pendingState.thinking;

        // Update model card role icons
        document.querySelectorAll('.model-role-icons .role-icon').forEach(icon => {
            const model = icon.dataset.model;
            const dataRole = icon.dataset.role;
            let isActive = false;

            if (dataRole === 'reviewer') {
                isActive = roles.reviewer1 === model || roles.reviewer2 === model;
            } else {
                const roleKey = this._dataRoleToKey(dataRole);
                if (roleKey) isActive = roles[roleKey] === model;
            }

            icon.classList.toggle('role-active', isActive);
        });

        // Update thinking icons in model cards
        document.querySelectorAll('.model-role-icons .thinking-icon').forEach(icon => {
            const model = icon.dataset.model;
            const hasRole = this._modelHasRole(model);
            const isActive = hasRole && !!thinking[model];

            icon.classList.toggle('thinking-active', isActive);
            icon.classList.toggle('thinking-inert', !hasRole);
        });

        // Update role summary table
        document.querySelectorAll('#role-summary .role-summary-row').forEach(row => {
            const roleKey = row.dataset.roleKey;
            if (!roleKey) return;

            const model = roles[roleKey] || null;
            const modelThinking = model ? !!thinking[model] : false;

            const iconEl = row.querySelector('.role-summary-icon');
            if (iconEl) iconEl.classList.toggle('icon-active', !!model);

            const cotEl = row.querySelector('.role-summary-cot');
            if (cotEl) cotEl.classList.toggle('cot-active', modelThinking);

            const valueEl = row.querySelector('.role-summary-value');
            if (valueEl) {
                if (model) {
                    valueEl.textContent = model;
                    valueEl.classList.remove('unassigned');
                } else {
                    valueEl.textContent = '-';
                    valueEl.classList.add('unassigned');
                }
            }
        });

        // Show/hide save toast
        const changed = JSON.stringify(this._pendingState) !== JSON.stringify(this._originalState);
        const toast = document.getElementById('save-roles-toast');
        if (toast) toast.classList.toggle('visible', changed);
    },

    saveRoles() {
        this._showConfirmDialog(
            'Save Role Configuration?',
            'This will update models.yaml with the new role assignments and CoT settings.',
            'Save Config',
            () => this._doSaveRoles(),
            false,
        );
    },

    async _doSaveRoles() {
        const toast = document.getElementById('save-roles-toast');
        const btn = toast ? toast.querySelector('.btn') : null;
        if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

        try {
            const resp = await fetch('/api/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({
                    roles: this._pendingState.roles,
                    thinking: this._pendingState.thinking,
                }),
            });

            if (resp.ok) {
                this._originalState = JSON.parse(JSON.stringify(this._pendingState));
                this._renderRoles();
            } else {
                const data = await resp.json();
                alert(data.detail || 'Save failed');
            }
        } catch (err) {
            alert('Network error: ' + err.message);
        }

        if (btn) { btn.disabled = false; btn.textContent = 'Save Changes'; }
    },
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => dvad.init());

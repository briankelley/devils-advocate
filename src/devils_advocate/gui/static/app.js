/* Devil's Advocate GUI — Client-side JS */

const dvad = {
    _sseSource: null,
    _revisionContent: '',
    _pendingRoles: {},
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
        this.initRolePills();
        this.initNewReviewForm();
        this.initTimeoutEditing();
        this.initMaxTokenEditing();
        this.initThinkingToggle();
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

        // Form submit → show interstitial
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            // Build FormData manually with path-based fields
            const fd = new FormData();
            fd.set('mode', document.getElementById('review-mode')?.value || 'plan');
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
    async cancelReview(reviewId) {
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

                // Hide generate button, add download link
                if (btn) btn.style.display = 'none';
                const footer = document.querySelector('.footer-actions');
                if (footer && !footer.querySelector('.download-revised-link')) {
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

    // ── Config tabs ──────────────────────────────────────────────────
    switchTab(tab) {
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

    async saveYaml() {
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

    // ── Role Icons ──────────────────────────────────────────────────
    initRolePills() {
        const icons = document.querySelectorAll('.role-icon');
        if (!icons.length || this._rolePillsInitialized) return;
        this._rolePillsInitialized = true;

        icons.forEach(icon => {
            icon.addEventListener('click', (e) => {
                e.stopPropagation();
                e.preventDefault();
                const model = icon.dataset.model;
                const role = icon.dataset.role;
                const isActive = icon.classList.contains('role-active');

                // Radio roles: only one model per role (except reviewer which is multi)
                const radioRoles = ['author', 'deduplication', 'integration_reviewer', 'normalization', 'revision'];

                if (radioRoles.includes(role)) {
                    // Deactivate all other icons for this role
                    document.querySelectorAll(`.role-icon[data-role="${role}"]`).forEach(p => {
                        p.classList.remove('role-active');
                        p.title = p.title.replace(' (assigned)', '');
                    });
                    // Activate this one (unless toggling off)
                    if (!isActive) {
                        icon.classList.add('role-active');
                        if (!icon.title.includes('(assigned)')) {
                            icon.title = icon.title + ' (assigned)';
                        }
                    }
                } else {
                    // Checkbox role (reviewer): toggle, max 2
                    icon.classList.toggle('role-active');
                    if (icon.classList.contains('role-active')) {
                        if (!icon.title.includes('(assigned)')) {
                            icon.title = icon.title + ' (assigned)';
                        }
                        // Cap at 2 reviewers: drop the oldest if over limit
                        const active = Array.from(document.querySelectorAll('.role-icon[data-role="reviewer"].role-active'));
                        if (active.length > 2) {
                            const oldest = active.find(el => el !== icon);
                            if (oldest) {
                                oldest.classList.remove('role-active');
                                oldest.title = oldest.title.replace(' (assigned)', '');
                            }
                        }
                    } else {
                        icon.title = icon.title.replace(' (assigned)', '');
                    }
                }

                this._markRolesDirty();
            });
        });
        this._updateRoleSummary();
    },

    _markRolesDirty() {
        const toast = document.getElementById('save-roles-toast');
        if (toast) toast.classList.add('visible');
        this._updateRoleSummary();
    },

    _updateRoleSummary() {
        const summary = document.getElementById('role-summary');
        if (!summary) return;

        // Scan all active role icons
        const roles = {};
        document.querySelectorAll('.role-icon.role-active').forEach(pill => {
            const model = pill.dataset.model;
            const role = pill.dataset.role;
            if (role === 'reviewer') {
                if (!roles.reviewers) roles.reviewers = [];
                roles.reviewers.push(model);
            } else {
                roles[role] = model;
            }
        });

        // Update single-assignment roles
        const singleRoles = ['author', 'deduplication', 'normalization', 'revision', 'integration_reviewer'];
        singleRoles.forEach(role => {
            const el = document.getElementById('rs-' + role);
            if (!el) return;
            if (roles[role]) {
                el.textContent = roles[role];
                el.className = 'role-summary-value';
            } else {
                el.textContent = 'unassigned';
                el.className = 'role-summary-value unassigned';
            }
        });

        // Update reviewers
        const rv1 = document.getElementById('rs-reviewer1');
        const rv2 = document.getElementById('rs-reviewer2');
        if (rv1) {
            if (roles.reviewers && roles.reviewers[0]) {
                rv1.textContent = roles.reviewers[0];
                rv1.className = 'role-summary-value';
            } else {
                rv1.textContent = 'unassigned';
                rv1.className = 'role-summary-value unassigned';
            }
        }
        if (rv2) {
            if (roles.reviewers && roles.reviewers[1]) {
                rv2.textContent = roles.reviewers[1];
                rv2.className = 'role-summary-value';
            } else {
                rv2.textContent = '\u2014';
                rv2.className = 'role-summary-value';
            }
        }

        // Update vendor icons + brain icons in role summary
        this._updateRoleSummaryIcons(roles);

        // Update thinking icon eligibility on model cards
        this._updateThinkingEligibility();
    },

    _updateRoleSummaryIcons(roles) {
        const roleKeys = [
            { key: 'author', prefix: 'author' },
            { key: 'reviewer1', prefix: 'reviewer1', get: (r) => r.reviewers?.[0] },
            { key: 'reviewer2', prefix: 'reviewer2', get: (r) => r.reviewers?.[1] },
            { key: 'deduplication', prefix: 'deduplication' },
            { key: 'normalization', prefix: 'normalization' },
            { key: 'revision', prefix: 'revision' },
            { key: 'integration_reviewer', prefix: 'integration_reviewer' },
        ];

        roleKeys.forEach(({ key, prefix, get }) => {
            const model = get ? get(roles) : roles[key];

            // Role icon: green when assigned
            const iconEl = document.getElementById('rsi-' + prefix);
            if (iconEl) {
                iconEl.classList.toggle('icon-active', !!model);
            }

            // CoT brain icon: active when model has thinking enabled
            const cotEl = document.getElementById('rsc-' + prefix);
            if (cotEl) {
                if (model && typeof modelThinking !== 'undefined' && modelThinking[model]) {
                    cotEl.classList.add('cot-active');
                } else {
                    cotEl.classList.remove('cot-active');
                }
            }
        });
    },

    _updateThinkingEligibility() {
        document.querySelectorAll('.thinking-icon').forEach(icon => {
            const model = icon.dataset.model;
            const hasActiveRole = document.querySelector(`.role-icon.role-active[data-model="${model}"]`);
            if (hasActiveRole) {
                icon.classList.add('thinking-eligible');
            } else {
                icon.classList.remove('thinking-eligible');
                icon.classList.remove('thinking-active');
            }
        });
    },

    async saveRoleAssignments() {
        const editor = document.getElementById('yaml-editor');
        if (!editor || typeof jsyaml === 'undefined') {
            alert('YAML editor or js-yaml library not available. Switch to Raw YAML tab to edit roles.');
            return;
        }

        let config;
        try {
            config = jsyaml.load(editor.value);
        } catch (e) {
            alert('Failed to parse current YAML: ' + e.message);
            return;
        }

        // Build roles from active pills
        const roles = {};
        const reviewers = [];

        document.querySelectorAll('.role-icon.role-active').forEach(icon => {
            const model = icon.dataset.model;
            const role = icon.dataset.role;

            if (role === 'reviewer') {
                reviewers.push(model);
            } else {
                roles[role] = model;
            }
        });

        if (reviewers.length > 0) {
            roles.reviewers = reviewers;
        }

        config.roles = roles;

        // Serialize and save
        const newYaml = jsyaml.dump(config, { lineWidth: -1, noRefs: true });
        editor.value = newYaml;

        try {
            const resp = await fetch('/api/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ yaml: newYaml }),
            });
            const data = await resp.json();

            const toast = document.getElementById('save-roles-toast');
            if (toast) toast.classList.remove('visible');

            const vr = document.getElementById('validation-result');
            if (resp.ok) {
                if (vr) vr.innerHTML = '<div class="issue issue-ok">Roles saved. Configuration is valid.</div>';
            } else {
                if (vr) vr.innerHTML = `<div class="issue issue-error">ERROR: ${data.detail}</div>`;
            }
        } catch (err) {
            alert('Network error: ' + err.message);
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
                                body: JSON.stringify({ model_name: modelName, [bodyKey]: null }),
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

    // ── Thinking Toggle ─────────────────────────────────────────────
    initThinkingToggle() {
        document.querySelectorAll('.thinking-icon').forEach(el => {
            el.addEventListener('click', async (e) => {
                e.stopPropagation();
                e.preventDefault();
                const model = el.dataset.model;

                // Only allow toggle if model has an active role
                if (!el.classList.contains('thinking-eligible') && !el.classList.contains('thinking-active')) return;

                const currentlyOn = el.classList.contains('thinking-active');
                const newValue = !currentlyOn;

                const resp = await fetch('/api/config/model-thinking', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-DVAD-Token': dvad.getToken(),
                    },
                    body: JSON.stringify({ model_name: model, thinking: newValue }),
                });

                if (resp.ok) {
                    el.classList.toggle('thinking-active', newValue);
                    el.title = 'Chain of Thought: ' + (newValue ? 'enabled' : 'disabled');
                    // Update modelThinking map if available
                    if (typeof modelThinking !== 'undefined') {
                        modelThinking[model] = newValue;
                    }
                    // Update brain icons in role summary
                    this._updateRoleSummary();
                }
            });
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

    async _clearEnvVar(name) {
        try {
            const resp = await fetch(`/api/config/env/${encodeURIComponent(name)}`, {
                method: 'DELETE',
                headers: { 'X-DVAD-Token': this.getToken() },
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
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => dvad.init());

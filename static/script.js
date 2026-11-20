marked.setOptions({
    highlight: function (code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    },
    langPrefix: 'hljs language-'
});

let lastUserQuery = "";
let hasChatted = false;
let currentSelectedMode = "auto";
let currentSessionId = 'session-' + Date.now();
const currentUsername = 'Bats Bat';
let currentUser = null;
let attachedFile = null;
let currentSpeakingUtterance = null;
let activeTypingTimer = null;

// Fixed typing speed: 24 chars/tick
const TYPING_CHARS_PER_TICK = 24;
const TYPING_INTERVAL_MS = 16;

const GREEK_WORD_TO_UNICODE = {
    alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε',
    zeta: 'ζ', eta: 'η', theta: 'θ', iota: 'ι', kappa: 'κ',
    lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', omicron: 'ο',
    pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ', upsilon: 'υ',
    phi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
    Alpha: 'Α', Beta: 'Β', Gamma: 'Γ', Delta: 'Δ', Epsilon: 'Ε',
    Zeta: 'Ζ', Eta: 'Η', Theta: 'Θ', Iota: 'Ι', Kappa: 'Κ',
    Lambda: 'Λ', Mu: 'Μ', Nu: 'Ν', Xi: 'Ξ', Omicron: 'Ο',
    Pi: 'Π', Rho: 'Ρ', Sigma: 'Σ', Tau: 'Τ', Upsilon: 'Υ',
    Phi: 'Φ', Chi: 'Χ', Psi: 'Ψ', Omega: 'Ω'
};

function convertDisplayBracketsToLatex(text) {
    if (!text) return "";
    // Convert standalone [ ... ] blocks that contain LaTeX commands or subscripts into $$ ... $$
    return text.replace(/(^|\n)\[\s*([\s\S]*?)\s*\](?=\n|$)/g, (match, prefix, inner) => {
        const looksLikeMath = /(\\[a-zA-Z]+|[_^]\{|\b[RTG]_(?:\\mu|\\nu|\{\\mu\\nu\})\b|=)/.test(inner);
        return looksLikeMath ? `${prefix}$$${inner}$$` : match;
    });
}

function normalizeGreekAndMath(text) {
    if (!text) return "";
    const withDisplayLatex = convertDisplayBracketsToLatex(text);

    // Split on fenced code blocks, inline code, and LaTeX math ($...$, $$...$$, \(...\), \[...\])
    const tokenPattern = /(```[\s\S]*?```|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|`[^`\n]+`)/g;
    const parts = withDisplayLatex.split(tokenPattern);

    return parts.map((part) => {
        if (!part) return "";
        // Preserve code blocks and LaTeX blocks untouched so KaTeX & highlight.js render cleanly
        if (
            part.startsWith('```') ||
            part.startsWith('$$') ||
            part.startsWith('$') ||
            part.startsWith('\\[') ||
            part.startsWith('\\(') ||
            part.startsWith('`')
        ) {
            return part;
        }
        // Replace standalone English Greek names in prose with real Unicode Greek letters
        return part.replace(/\b([A-Za-z]{2,8})\b/g, (match, word) => {
            return Object.prototype.hasOwnProperty.call(GREEK_WORD_TO_UNICODE, word)
                ? GREEK_WORD_TO_UNICODE[word]
                : match;
        });
    }).join('');
}

function autoResizeTextarea(el) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function setSidebarOpen(isOpen) {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    if (!sidebar || !backdrop) return;
    sidebar.classList.toggle('collapsed', !isOpen);
    backdrop.classList.toggle('active', isOpen);
}

function toggleSidebar(event) {
    if (event) event.stopPropagation();
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    const willOpen = sidebar.classList.contains('collapsed');
    closeAllMenus();
    setSidebarOpen(willOpen);
}

function collapseSidebar() {
    setSidebarOpen(false);
}

function openAuthModal() {
    collapseSidebar();
    document.getElementById('authModal').style.display = 'flex';
}

function closeAuthModal() {
    document.getElementById('authModal').style.display = 'none';
}

function switchAuthTab(tab) {
    document.getElementById('authAlert').innerText = '';
    const isSignIn = tab === 'signin';
    document.getElementById('tabSignInBtn').classList.toggle('active', isSignIn);
    document.getElementById('tabSignUpBtn').classList.toggle('active', !isSignIn);
    document.getElementById('formSignIn').style.display = isSignIn ? 'block' : 'none';
    document.getElementById('formSignUp').style.display = isSignIn ? 'none' : 'block';
}

function backToStep1() {
    document.getElementById('signupStep1').style.display = 'block';
    document.getElementById('signupStep2').style.display = 'none';
}

async function checkAuth() {
    try {
        const res = await fetch('/api/me');
        if (res.ok) {
            const data = await res.json();
            currentUser = data.user;
        }
    } catch (e) {
        // Guest mode
    }
    updateUserUI();
    loadUserHistory();
}

function updateUserUI() {
    const nameEl = document.getElementById('userNameLabel');
    const avatarEl = document.getElementById('userAvatar');
    const authBtn = document.getElementById('sidebarAuthBtn');

    if (currentUser) {
        nameEl.innerText = currentUser.name;
        avatarEl.innerText = currentUser.name.charAt(0).toUpperCase();
        authBtn.innerText = "Sign Out";
        authBtn.onclick = handleLogout;
    } else {
        nameEl.innerText = "Guest User";
        avatarEl.innerText = "G";
        authBtn.innerText = "Sign In";
        authBtn.onclick = openAuthModal;
    }
}

async function handleSignIn() {
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value.trim();
    const alertDiv = document.getElementById('authAlert');

    if (!email || !password) {
        alertDiv.innerText = "Please fill in email and password.";
        return;
    }

    try {
        const res = await fetch('/api/signin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        const data = await res.json();

        if (res.ok) {
            currentUser = data.user;
            closeAuthModal();
            updateUserUI();
            loadUserHistory();
        } else {
            alertDiv.innerText = data.error || "Sign in failed.";
        }
    } catch (e) {
        alertDiv.innerText = "Network error signing in.";
    }
}

async function handleSendSignupOtp() {
    const name = document.getElementById('regName').value.trim();
    const email = document.getElementById('regEmail').value.trim();
    const password = document.getElementById('regPassword').value.trim();
    const alertDiv = document.getElementById('authAlert');

    if (!name || !email || !password) {
        alertDiv.innerText = "All fields are required.";
        return;
    }

    try {
        const res = await fetch('/api/send-signup-otp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, email, password })
        });
        const data = await res.json();

        if (res.ok) {
            document.getElementById('signupStep1').style.display = 'none';
            document.getElementById('signupStep2').style.display = 'block';
            alertDiv.innerText = '';
        } else {
            alertDiv.innerText = data.error || "Failed to send code.";
        }
    } catch (e) {
        alertDiv.innerText = "Network error.";
    }
}

async function handleVerifySignupOtp() {
    const email = document.getElementById('regEmail').value.trim();
    const otp = document.getElementById('regOtp').value.trim();
    const alertDiv = document.getElementById('authAlert');

    if (otp.length !== 6) {
        alertDiv.innerText = "Please enter the 6-digit code.";
        return;
    }

    try {
        const res = await fetch('/api/verify-signup-otp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, otp })
        });
        const data = await res.json();

        if (res.ok) {
            currentUser = data.user;
            closeAuthModal();
            updateUserUI();
            loadUserHistory();
        } else {
            alertDiv.innerText = data.error || "Invalid OTP code.";
        }
    } catch (e) {
        alertDiv.innerText = "Verification network error.";
    }
}

async function handleLogout() {
    await fetch('/api/logout', { method: 'POST' });
    currentUser = null;
    updateUserUI();
    startNewChat();
    document.getElementById('recentChatsList').innerHTML = `<div class="chat-history-item muted-item">No recent conversations</div>`;
}

function promoteImageLinksToImages(markdownText) {
    if (!markdownText) return "";
    return markdownText.replace(
        /(^|\n)(https?:\/\/[^\s)]+\.(?:png|jpe?g|webp|gif|avif|svg)(?:\?[^\s)]*)?)(?=\n|$)/gi,
        (match, prefix, url) => `${prefix}![Image](${url})`
    );
}

function getLinkHostBadge(url) {
    try {
        const parsed = new URL(url);
        return parsed.hostname.replace(/^www\./, '');
    } catch {
        return url;
    }
}

function createClickableLinkCard(url, labelText) {
    const link = document.createElement('a');
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "bot-link-card";

    const icon = document.createElement('i');
    icon.className = "fa-solid fa-arrow-up-right-from-square";

    const text = document.createElement('span');
    const host = getLinkHostBadge(url);
    text.textContent = labelText && labelText !== url ? `${labelText} (${host})` : host;

    link.appendChild(icon);
    link.appendChild(text);
    return link;
}

function parseMediaEmbeds(htmlContent) {
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = htmlContent;

    tempDiv.querySelectorAll('a').forEach((a) => {
        const href = a.href || "";
        const isDirectImageUrl = /\.(?:png|jpe?g|webp|gif|avif|svg)(?:\?.*)?$/i.test(href);
        if (isDirectImageUrl && /^https?:\/\//i.test(href)) {
            const img = document.createElement('img');
            img.src = href;
            img.alt = a.textContent || "Displayed image";
            img.loading = "lazy";
            img.style.cssText = "max-width:100%;max-height:420px;border-radius:12px;margin:10px 0;display:block;object-fit:contain;";
            img.onerror = () => img.replaceWith(createClickableLinkCard(href, a.textContent));
            a.replaceWith(img);
        } else if (/^https?:\/\//i.test(href)) {
            a.replaceWith(createClickableLinkCard(href, a.textContent.trim()));
        }
    });

    tempDiv.querySelectorAll('img').forEach((img) => {
        img.loading = "lazy";
        img.style.cssText = "max-width:100%;max-height:420px;border-radius:12px;margin:10px 0;display:block;object-fit:contain;";
        img.onerror = () => {
            img.replaceWith(createClickableLinkCard(img.src, img.alt));
        };
    });

    return tempDiv.innerHTML;
}

async function loadUserHistory() {
    const targetIdentifier = currentUser ? currentUser.email : currentUsername;
    try {
        const res = await fetch(`/sessions/${encodeURIComponent(targetIdentifier)}`);
        const sessions = await res.json();
        const recentList = document.getElementById('recentChatsList');

        if (Array.isArray(sessions) && sessions.length > 0) {
            recentList.innerHTML = '';
            sessions.forEach((s) => {
                recentList.innerHTML += `
                    <div class="chat-history-item" onclick="switchSession('${s.session_id}')">
                        <span class="chat-title-text">${escapeHtml(s.title)}</span>
                        <button class="delete-chat-btn" onclick="deleteHistorySession(event, '${s.session_id}')" title="Delete chat">
                            <i class="fa-solid fa-xmark"></i>
                        </button>
                    </div>
                `;
            });
        } else {
            recentList.innerHTML = `<div class="chat-history-item muted-item">No recent conversations</div>`;
        }
    } catch (e) {
        console.error("Could not load history", e);
    }
}

async function switchSession(sessionId) {
    collapseSidebar();
    currentSessionId = sessionId;
    const chatBox = document.getElementById('chatBox');
    chatBox.innerHTML = '';

    try {
        const res = await fetch(`/history/${sessionId}`);
        const messages = await res.json();

        messages.forEach((m) => {
            if (m.role === 'user') {
                chatBox.innerHTML += `
                    <div class="message-wrapper user-wrapper">
                        <div class="message user-message">${escapeHtml(m.content)}</div>
                    </div>
                `;
            } else {
                const uniqueId = 'msg-' + Math.random().toString(36).slice(2, 10);
                chatBox.innerHTML += `
                    <div class="message-wrapper bot-wrapper">
                        <div id="${uniqueId}" class="message bot-message"></div>
                        ${createResponseActionBar(uniqueId)}
                    </div>
                `;
                const container = document.getElementById(uniqueId);
                const formatted = promoteImageLinksToImages(normalizeGreekAndMath(m.content));
                container.innerHTML = parseMediaEmbeds(marked.parse(formatted));
                renderMathAndHighlight(container);
            }
        });

        hasChatted = messages.length > 0;
        chatBox.scrollTop = 0;
    } catch (e) {
        console.error("Could not load session history", e);
    }
}

async function deleteHistorySession(event, sessionId) {
    event.stopPropagation();
    if (confirm("Delete this chat history?")) {
        try {
            const response = await fetch(`/session/${sessionId}`, { method: 'DELETE' });
            if (response.ok) {
                if (currentSessionId === sessionId) {
                    startNewChat();
                }
                loadUserHistory();
            }
        } catch (err) {
            console.error("Error deleting session", err);
        }
    }
}

function createResponseActionBar(msgId) {
    return `
        <div class="response-action-bar">
            <button class="response-action-btn" onclick="rateResponse(this, 'up')" title="Good response"><i class="fa-regular fa-thumbs-up"></i></button>
            <button class="response-action-btn" onclick="rateResponse(this, 'down')" title="Bad response"><i class="fa-regular fa-thumbs-down"></i></button>
            <button class="response-action-btn" onclick="retryLastResponse()" title="Regenerate response"><i class="fa-solid fa-rotate-right"></i></button>
            <button class="response-action-btn" onclick="copyMessageText('${msgId}')" title="Copy text"><i class="fa-regular fa-copy"></i></button>
            <button class="response-action-btn" onclick="shareMessage('${msgId}')" title="Share"><i class="fa-solid fa-arrow-up-from-bracket"></i></button>
            <button class="response-action-btn" onclick="toggleResponseMoreMenu(event, '${msgId}')" title="More options"><i class="fa-solid fa-ellipsis"></i></button>

            <div id="menu-${msgId}" class="response-more-menu">
                <div class="response-menu-item" onclick="branchNewChat('${msgId}')"><i class="fa-solid fa-code-branch"></i> Branch in new chat</div>
                <div class="response-menu-item" onclick="listenToMessage('${msgId}')"><i class="fa-solid fa-volume-high"></i> Listen</div>
                <div class="response-menu-item" onclick="exportToDocs('${msgId}')"><i class="fa-regular fa-file-word"></i> Export to Docs</div>
                <div class="response-menu-item" onclick="draftInGmail('${msgId}')"><i class="fa-regular fa-envelope"></i> Draft in Gmail</div>
                <div class="response-menu-item" onclick="showResponseDetails('${msgId}')"><i class="fa-solid fa-circle-info"></i> Response details</div>
            </div>
        </div>
    `;
}

function toggleResponseMoreMenu(event, msgId) {
    event.stopPropagation();
    const targetMenu = document.getElementById(`menu-${msgId}`);
    const wasOpen = targetMenu && targetMenu.classList.contains('active');
    closeAllMenus();
    if (targetMenu && !wasOpen) {
        targetMenu.classList.add('active');
    }
}

function closeAllMenus() {
    document.querySelectorAll('.response-more-menu').forEach((m) => m.classList.remove('active'));
    const plusMenu = document.getElementById('plusPopupMenu');
    if (plusMenu) plusMenu.classList.remove('active');
    const topMenu = document.getElementById('topDropdownMenu');
    const modelMenu = document.getElementById('modelDropdownMenu');
    if (topMenu) topMenu.style.display = 'none';
    if (modelMenu) modelMenu.style.display = 'none';
}

function rateResponse(btn) {
    btn.classList.toggle('active');
}

function retryLastResponse() {
    if (lastUserQuery) {
        sendMessage(lastUserQuery);
    }
}

function copyMessageText(msgId) {
    const text = document.getElementById(msgId).innerText;
    navigator.clipboard.writeText(text);
}

function shareMessage(msgId) {
    const text = document.getElementById(msgId).innerText;
    if (navigator.share) {
        navigator.share({ title: 'Bats AI Response', text });
    } else {
        copyMessageText(msgId);
    }
}

function branchNewChat() {
    startNewChat();
}

function listenToMessage(msgId) {
    if ('speechSynthesis' in window) {
        if (window.speechSynthesis.speaking) {
            window.speechSynthesis.cancel();
            return;
        }
        const text = document.getElementById(msgId).innerText;
        currentSpeakingUtterance = new SpeechSynthesisUtterance(text);
        window.speechSynthesis.speak(currentSpeakingUtterance);
    }
}

function exportToDocs(msgId) {
    const text = document.getElementById(msgId).innerText;
    const blob = new Blob([text], { type: "application/msword" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "Bats_AI_Response.doc";
    a.click();
    URL.revokeObjectURL(url);
}

function draftInGmail(msgId) {
    const text = encodeURIComponent(document.getElementById(msgId).innerText);
    window.open(`[https://mail.google.com/mail/?view=cm&fs=1&body=$](https://mail.google.com/mail/?view=cm&fs=1&body=$){text}`, '_blank');
}

function showResponseDetails(msgId) {
    const text = document.getElementById(msgId).innerText;
    alert(`Characters: ${text.length}\nMode: ${currentSelectedMode}\nSession: ${currentSessionId}`);
}

function togglePlusMenu(event) {
    event.stopPropagation();
    const menu = document.getElementById('plusPopupMenu');
    const wasOpen = menu.classList.contains('active');
    closeAllMenus();
    if (!wasOpen) menu.classList.add('active');
}

function triggerFileUpload(acceptType) {
    const fileInput = document.getElementById('hiddenFileInput');
    fileInput.accept = acceptType;
    fileInput.click();
    closeAllMenus();
}

function handleFileSelected(event) {
    const file = event.target.files[0];
    if (file) {
        attachedFile = file;
        document.getElementById('attachmentName').innerText = file.name;
        document.getElementById('attachmentChip').style.display = 'inline-flex';
    }
}

function clearAttachedFile() {
    attachedFile = null;
    document.getElementById('hiddenFileInput').value = '';
    document.getElementById('attachmentChip').style.display = 'none';
}

function selectCreateImageMenu() {
    closeAllMenus();
    const input = document.getElementById('userInput');
    input.value = "Create an image of ";
    autoResizeTextarea(input);
    input.focus();
}

function selectCreateVideoMenu() {
    closeAllMenus();
    const input = document.getElementById('userInput');
    input.value = "Create a video concept and script for ";
    autoResizeTextarea(input);
    input.focus();
}

function toggleModelDropdown(event) {
    event.stopPropagation();
    const menu = document.getElementById('modelDropdownMenu');
    const wasOpen = menu.style.display === 'block';
    closeAllMenus();
    if (!wasOpen) menu.style.display = 'block';
}

function selectMode(modeKey, modeName) {
    currentSelectedMode = modeKey;
    document.getElementById('currentModeLabel').innerText = modeName;
    document.getElementById('modelDropdownMenu').style.display = 'none';
}

function toggleTopMenu(event) {
    event.stopPropagation();
    const menu = document.getElementById('topDropdownMenu');
    const wasOpen = menu.style.display === 'block';
    closeAllMenus();
    if (!wasOpen) menu.style.display = 'block';
}

document.addEventListener('click', (e) => {
    closeAllMenus();
    const sidebar = document.getElementById('sidebar');
    const openBtn = document.getElementById('openSidebarBtn');
    if (
        sidebar &&
        !sidebar.classList.contains('collapsed') &&
        !sidebar.contains(e.target) &&
        (!openBtn || !openBtn.contains(e.target))
    ) {
        collapseSidebar();
    }
});

function startNewChat() {
    if (activeTypingTimer) {
        clearTimeout(ac3tiveTypingTimer);
        activeTypingTimer = null;
    }
    currentSessionId = 'session-' + Date.now();
    hasChatted = false;
    clearAttachedFile();
    collapseSidebar();

    const inputEl = document.getElementById('userInput');
    inputEl.value = '';
    autoResizeTextarea(inputEl);

    document.getElementById('chatBox').innerHTML = `
        <div class="welcome-state" id="welcomeState">
          <div class="multi-color-brand" aria-label="BATS AI">
            <span class="c-b">B</span><span class="c-a1">A</span><span class="c-t">T</span><span class="c-s">S</span><span class="c-space"> </span><span class="c-a2">A</span><span class="c-i">I</span>
          </div>
          <h1 class="gradient-start-text">WHERE SHOULD WE START?</h1>
        </div>
    `;
}

function openSection(sectionName) {
    collapseSidebar();
    const chatBox = document.getElementById('chatBox');
    chatBox.innerHTML = `
        <div class="section-panel">
            <h2>${escapeHtml(sectionName)}</h2>
            <p>Manage your ${escapeHtml(sectionName.toLowerCase())} items in Bats AI.</p>
            <button class="pill-action-btn" onclick="startNewChat()">
                <i class="fa-solid fa-arrow-left"></i> Back to chat
            </button>
        </div>
    `;
}

function openCustomizeSection() {
    collapseSidebar();
    const chatBox = document.getElementById('chatBox');
    chatBox.innerHTML = `
        <div class="section-panel">
            <h2>Appearance</h2>
            <p>Choose dark or light theme.</p>
            <div class="choice-row">
                <button class="pill-action-btn" onclick="setAppTheme('theme-black')"><i class="fa-solid fa-moon"></i> Dark mode</button>
                <button class="pill-action-btn" onclick="setAppTheme('theme-white')"><i class="fa-solid fa-sun"></i> Light mode</button>
            </div>
            <button class="pill-action-btn" onclick="startNewChat()">
                <i class="fa-solid fa-arrow-left"></i> Back to chat
            </button>
        </div>
    `;
}

function setAppTheme(themeName) {
    const allowedTheme = themeName === 'theme-white' ? 'theme-white' : 'theme-black';
    document.body.className = allowedTheme;
    localStorage.setItem('bats_ai_theme', allowedTheme);
}

function toggleMicrophone() {
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
        alert("Speech recognition is not supported in this browser.");
        return;
    }
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    const micBtn = document.getElementById('micBtn');

    recognition.onstart = () => micBtn.classList.add('recording');
    recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        const inputEl = document.getElementById('userInput');
        inputEl.value = transcript;
        autoResizeTextarea(inputEl);
    };
    recognition.onerror = () => micBtn.classList.remove('recording');
    recognition.onend = () => micBtn.classList.remove('recording');
    recognition.start();
}

function renderMathAndHighlight(container) {
    container.querySelectorAll('pre').forEach((preBlock) => {
        const codeEl = preBlock.querySelector('code');
        if (codeEl && !preBlock.parentElement.classList.contains('code-block-wrapper')) {
            hljs.highlightElement(codeEl);

            const wrapper = document.createElement('div');
            wrapper.className = 'code-block-wrapper';

            const copyBtn = document.createElement('button');
            copyBtn.type = 'button';
            copyBtn.className = 'code-copy-btn';
            copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Copy';

            copyBtn.onclick = () => {
                const codeText = codeEl ? codeEl.innerText : preBlock.innerText;
                navigator.clipboard.writeText(codeText).then(() => {
                    copyBtn.innerHTML = '<i class="fa-solid fa-check"></i> Copied';
                    setTimeout(() => {
                        copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Copy';
                    }, 1800);
                });
            };

            preBlock.parentNode.insertBefore(wrapper, preBlock);
            wrapper.appendChild(preBlock);
            wrapper.appendChild(copyBtn);
        }
    });

    if (typeof renderMathInElement === 'function') {
        renderMathInElement(container, {
            delimiters: [
                { left: '$$', right: '$$', display: true },
                { left: '$', right: '$', display: false },
                { left: '\\(', right: '\\)', display: false },
                { left: '\\[', right: '\\]', display: true }
            ],
            throwOnError: false
        });
    }
}

async function sendMessage(customText = null) {
    if (activeTypingTimer) {
        clearTimeout(activeTypingTimer);
        activeTypingTimer = null;
    }

    const inputField = document.getElementById('userInput');
    const chatBox = document.getElementById('chatBox');
    const rawTyped = customText !== null ? customText : inputField.value.trim();
    const currentFile = attachedFile;

    if (!rawTyped && !currentFile) return;

    const displayMessage = currentFile
        ? `[Attached File: ${currentFile.name}] ${rawTyped}`.trim()
        : rawTyped;

    lastUserQuery = displayMessage;
    hasChatted = true;
    collapseSidebar();

    const welcomeState = document.getElementById('welcomeState');
    if (welcomeState) welcomeState.remove();

    chatBox.insertAdjacentHTML('beforeend', `
        <div class="message-wrapper user-wrapper">
            <div class="message user-message">${escapeHtml(displayMessage)}</div>
        </div>
    `);

    if (customText === null) {
        inputField.value = '';
        autoResizeTextarea(inputField);
    }
    clearAttachedFile();

    const loadingId = 'loading-' + Date.now();
    chatBox.insertAdjacentHTML('beforeend', `
        <div id="${loadingId}" class="search-bubble">
            <div class="spinner"></div>
            <span>Bats AI is thinking...</span>
        </div>
    `);

    try {
        const formData = new FormData();
        const promptToSend = rawTyped || (currentFile ? `Please read ${currentFile.name} and explain its contents.` : displayMessage);
        formData.append('message', promptToSend);
        formData.append('mode', currentSelectedMode);
        formData.append('session_id', currentSessionId);
        formData.append('web_search', document.getElementById('webSearchToggle')?.checked ? 'true' : 'false');
        if (currentFile) {
            formData.append('file', currentFile, currentFile.name);
        }

        const response = await fetch('/chat', {
            method: 'POST',
            body: formData
        });
        const data = await response.json();

        const loader = document.getElementById(loadingId);
        if (loader) loader.remove();

        if (data.reply) {
            const uniqueId = 'msg-' + Date.now();
            const wrapperId = 'wrap-' + Date.now();
            chatBox.insertAdjacentHTML('beforeend', `
                <div id="${wrapperId}" class="message-wrapper bot-wrapper">
                    <div id="${uniqueId}" class="message bot-message typing-cursor"></div>
                    ${createResponseActionBar(uniqueId)}
                </div>
            `);

            const botWrapper = document.getElementById(wrapperId);
            const botMsgContainer = document.getElementById(uniqueId);

            // Start view at the beginning of the new answer instead of chasing bottom scroll
            if (botWrapper) {
                chatBox.scrollTop = Math.max(0, botWrapper.offsetTop - 12);
            }

            const fullText = promoteImageLinksToImages(normalizeGreekAndMath(data.reply));
            let charIndex = 0;

            function finishReplyRender() {
                botMsgContainer.classList.remove('typing-cursor');
                botMsgContainer.innerHTML = parseMediaEmbeds(marked.parse(fullText));
                renderMathAndHighlight(botMsgContainer);

                if (data.web_search_used && Array.isArray(data.sources) && data.sources.length) {
                    const sourceList = document.createElement('div');
                    sourceList.className = 'web-sources';

                    const sourceHeading = document.createElement('div');
                    sourceHeading.className = 'web-sources-label';
                    sourceHeading.textContent = 'Web sources';
                    sourceList.appendChild(sourceHeading);

                    data.sources.forEach((source) => {
                        if (!source.url || !/^https?:\/\//i.test(source.url)) return;
                        const card = createClickableLinkCard(source.url, source.title || source.url);
                        card.classList.add('web-source-card');
                        sourceList.appendChild(card);
                    });

                    if (sourceList.children.length > 1) {
                        botMsgContainer.appendChild(sourceList);
                    }
                }

                if (data.history_saved) {
                    loadUserHistory();
                }
            }

            function typeWriter() {
                if (charIndex < fullText.length) {
                    charIndex = Math.min(charIndex + TYPING_CHARS_PER_TICK, fullText.length);
                    const partialText = fullText.slice(0, charIndex);
                    // Plain text / markdown preview while typing; full HTML + KaTeX + highlight renders cleanly at completion
                    botMsgContainer.innerHTML = marked.parse(partialText);
                    activeTypingTimer = setTimeout(typeWriter, TYPING_INTERVAL_MS);
                } else {
                    activeTypingTimer = null;
                    finishReplyRender();
                }
            }

            typeWriter();
        } else {
            chatBox.insertAdjacentHTML('beforeend', `<div class="message bot-message" style="color:#ef4444;">Error: ${escapeHtml(data.error || 'Unknown error')}</div>`);
        }
    } catch (err) {
        const loader = document.getElementById(loadingId);
        if (loader) loader.remove();
        chatBox.insertAdjacentHTML('beforeend', `<div class="message bot-message" style="color:#ef4444;">Network error connecting to server.</div>`);
    }
}

function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
}

function escapeHtml(text) {
    return String(text || '')
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

window.addEventListener('DOMContentLoaded', () => {
    setSidebarOpen(false);
    checkAuth();
    const savedTheme = localStorage.getItem('bats_ai_theme');
    document.body.className = savedTheme === 'theme-white' ? 'theme-white' : 'theme-black';
});
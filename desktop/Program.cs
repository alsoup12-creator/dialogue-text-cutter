using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace TextCut.Desktop;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        using var singleInstance = new Mutex(true, "Local\\TextCutDesktop-6D3CB15B", out var createdNew);
        if (!createdNew)
        {
            MessageBox.Show("对白剪辑台已经在运行。", "对白剪辑台", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        ApplicationConfiguration.Initialize();
        Application.Run(new MainForm());
    }
}

internal sealed class MainForm : Form
{
    private readonly WebView2 _webView = new() { Dock = DockStyle.Fill };
    private readonly Panel _loadingPanel = new() { Dock = DockStyle.Fill, BackColor = Color.FromArgb(245, 242, 234) };
    private readonly Label _loadingLabel = new()
    {
        AutoSize = false,
        Dock = DockStyle.Fill,
        TextAlign = ContentAlignment.MiddleCenter,
        Font = new Font("微软雅黑", 15F),
        ForeColor = Color.FromArgb(31, 83, 68),
        Text = "正在启动对白剪辑台…"
    };

    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(2) };
    private readonly HttpClient _sourceImportHttp = new() { Timeout = TimeSpan.FromSeconds(20) };
    private Process? _backend;
    private string? _backendUrl;
    private string? _desktopToken;
    private string? _appRoot;
    private bool _nativeImportEnabled = true;
    private bool _sourceImportInProgress;
    private bool _allowClose;
    private bool _closeInProgress;

    public MainForm()
    {
        Text = "对白剪辑台";
        StartPosition = FormStartPosition.CenterScreen;
        Width = 1500;
        Height = 920;
        MinimumSize = new Size(1040, 700);
        AllowDrop = true;
        BackColor = _loadingPanel.BackColor;
        _loadingPanel.Controls.Add(_loadingLabel);
        Controls.Add(_webView);
        Controls.Add(_loadingPanel);
        _loadingPanel.BringToFront();
        // The WebView itself fills the form, so it must be an enabled Windows
        // drop target. Its WinForms events still expose DataFormats.FileDrop,
        // which lets the backend reference the original path instead of using
        // the browser's byte-upload path.
        _webView.AllowExternalDrop = true;
        DragEnter += OnNativeDragEnter;
        DragLeave += OnNativeDragLeave;
        DragDrop += OnNativeDragDrop;
        _webView.DragEnter += OnNativeDragEnter;
        _webView.DragLeave += OnNativeDragLeave;
        _webView.DragDrop += OnNativeDragDrop;
        Shown += async (_, _) => await StartAsync();
        FormClosing += OnFormClosing;
    }

    private async Task StartAsync()
    {
        try
        {
            _appRoot = FindAppRoot();
            var port = GetAvailablePort();
            _backendUrl = $"http://127.0.0.1:{port}";
            _desktopToken = Guid.NewGuid().ToString("N");
            StartBackend(_appRoot, port, _desktopToken);
            await WaitForBackendAsync(_backendUrl);
            await InitializeWebViewAsync(_appRoot, _backendUrl);
            _loadingPanel.Visible = false;
            _webView.Focus();
        }
        catch (Exception exception)
        {
            _loadingLabel.Text = "启动失败\r\n\r\n" + exception.Message + "\r\n\r\n请将此界面截图发给 Codex。";
            _loadingLabel.ForeColor = Color.FromArgb(174, 68, 50);
        }
    }

    private static string FindAppRoot()
    {
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        for (var depth = 0; depth < 8 && current is not null; depth++, current = current.Parent)
        {
            if (File.Exists(Path.Combine(current.FullName, "app.py")) && Directory.Exists(Path.Combine(current.FullName, "static")))
                return current.FullName;
        }
        throw new DirectoryNotFoundException("找不到 app.py 和 static 目录。请不要单独移动桌面端 EXE。");
    }

    private void StartBackend(string appRoot, int port, string token)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = "python",
            WorkingDirectory = appRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        startInfo.ArgumentList.Add(Path.Combine(appRoot, "app.py"));
        startInfo.ArgumentList.Add("--host");
        startInfo.ArgumentList.Add("127.0.0.1");
        startInfo.ArgumentList.Add("--port");
        startInfo.ArgumentList.Add(port.ToString());
        startInfo.Environment["TEXTCUT_SKIP_BROWSER"] = "1";
        startInfo.Environment["TEXTCUT_DESKTOP_TOKEN"] = token;

        _backend = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        _backend.OutputDataReceived += (_, eventArgs) => AppendLog(eventArgs.Data);
        _backend.ErrorDataReceived += (_, eventArgs) => AppendLog(eventArgs.Data);
        if (!_backend.Start())
            throw new InvalidOperationException("无法启动本地剪辑后台。");
        _backend.BeginOutputReadLine();
        _backend.BeginErrorReadLine();
    }

    private void AppendLog(string? line)
    {
        if (string.IsNullOrWhiteSpace(line) || string.IsNullOrWhiteSpace(_appRoot)) return;
        try
        {
            var folder = Path.Combine(_appRoot, ".data");
            Directory.CreateDirectory(folder);
            File.AppendAllText(Path.Combine(folder, "desktop.log"), $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {line}{Environment.NewLine}");
        }
        catch
        {
            // Logging must never interrupt editing.
        }
    }

    private async Task WaitForBackendAsync(string url)
    {
        var deadline = DateTime.UtcNow.AddSeconds(25);
        Exception? lastError = null;
        while (DateTime.UtcNow < deadline)
        {
            if (_backend is { HasExited: true })
                throw new InvalidOperationException("本地剪辑后台提前退出。");
            try
            {
                using var response = await _http.GetAsync(url + "/api/config");
                if (response.IsSuccessStatusCode) return;
            }
            catch (Exception exception)
            {
                lastError = exception;
            }
            await Task.Delay(200);
        }
        throw new TimeoutException("本地剪辑后台启动超时。" + (lastError is null ? "" : $" {lastError.Message}"));
    }

    private async Task InitializeWebViewAsync(string appRoot, string url)
    {
        var userData = Path.Combine(appRoot, ".data", "webview2");
        var environment = await CoreWebView2Environment.CreateAsync(null, userData);
        await _webView.EnsureCoreWebView2Async(environment);
        var settings = _webView.CoreWebView2.Settings;
        settings.AreDefaultContextMenusEnabled = false;
        settings.AreDevToolsEnabled = false;
        settings.AreBrowserAcceleratorKeysEnabled = false;
        settings.IsStatusBarEnabled = false;
        settings.IsZoomControlEnabled = false;
        settings.IsGeneralAutofillEnabled = false;
        settings.IsPasswordAutosaveEnabled = false;
        _webView.CoreWebView2.WebMessageReceived += OnWebMessageReceived;
        _webView.CoreWebView2.NewWindowRequested += (_, eventArgs) => eventArgs.Handled = true;
        _webView.CoreWebView2.NavigationStarting += (_, eventArgs) =>
        {
            if (!eventArgs.Uri.StartsWith(url, StringComparison.OrdinalIgnoreCase))
                eventArgs.Cancel = true;
        };
        _webView.Source = new Uri(url);
    }

    private void OnWebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs eventArgs)
    {
        if (_backendUrl is null || !eventArgs.Source.StartsWith(_backendUrl, StringComparison.OrdinalIgnoreCase)) return;
        try
        {
            using var document = JsonDocument.Parse(eventArgs.WebMessageAsJson);
            if (!document.RootElement.TryGetProperty("type", out var typeValue)) return;
            var type = typeValue.GetString();
            if (type == "native-bridge-handshake")
            {
                PostNativeMessage(new { type = "native-bridge-ready" });
                return;
            }
            if (type == "set-native-import-enabled" && document.RootElement.TryGetProperty("enabled", out var enabled))
            {
                _nativeImportEnabled = enabled.GetBoolean();
                _webView.AllowExternalDrop = _nativeImportEnabled && !_sourceImportInProgress;
                return;
            }
            if (type == "pick-native-source-file" && _nativeImportEnabled)
            {
                BeginInvoke(PickNativeSourceFile);
            }
        }
        catch (JsonException)
        {
            // Ignore malformed page messages. The page itself is restricted to the local backend URL.
        }
    }

    private void PickNativeSourceFile()
    {
        if (_sourceImportInProgress) return;
        using var picker = new OpenFileDialog
        {
            Title = "选择要剪辑的视频",
            Filter = "视频文件|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.ts;*.mts;*.m2ts;*.wmv;*.flv;*.3gp|所有文件|*.*",
            CheckFileExists = true,
            Multiselect = false,
        };
        if (picker.ShowDialog(this) == DialogResult.OK)
            _ = ImportNativeSourceAsync(picker.FileName);
    }

    private void OnNativeDragEnter(object? sender, DragEventArgs eventArgs)
    {
        if (!_nativeImportEnabled || _sourceImportInProgress || !TryGetDroppedVideo(eventArgs.Data, out _))
        {
            eventArgs.Effect = DragDropEffects.None;
            return;
        }
        eventArgs.Effect = DragDropEffects.Copy;
        PostNativeMessage(new { type = "native-drag-state", active = true });
    }

    private void OnNativeDragLeave(object? sender, EventArgs eventArgs)
    {
        PostNativeMessage(new { type = "native-drag-state", active = false });
    }

    private void OnNativeDragDrop(object? sender, DragEventArgs eventArgs)
    {
        PostNativeMessage(new { type = "native-drag-state", active = false });
        if (_nativeImportEnabled && !_sourceImportInProgress && TryGetDroppedVideo(eventArgs.Data, out var sourcePath))
            _ = ImportNativeSourceAsync(sourcePath);
    }

    private static bool TryGetDroppedVideo(IDataObject? data, out string sourcePath)
    {
        sourcePath = "";
        if (data?.GetDataPresent(DataFormats.FileDrop) != true) return false;
        if (data.GetData(DataFormats.FileDrop) is not string[] files || files.Length != 1) return false;
        if (!File.Exists(files[0])) return false;
        sourcePath = files[0];
        return true;
    }

    private async Task ImportNativeSourceAsync(string sourcePath)
    {
        if (_sourceImportInProgress || _backendUrl is null) return;
        _sourceImportInProgress = true;
        _webView.AllowExternalDrop = false;
        PostNativeMessage(new { type = "native-source-importing" });
        try
        {
            if (!File.Exists(sourcePath)) throw new FileNotFoundException("找不到拖入的视频文件。", sourcePath);
            using var request = new HttpRequestMessage(HttpMethod.Post, _backendUrl + "/api/projects/from-path")
            {
                Content = new StringContent(JsonSerializer.Serialize(new { path = sourcePath }), Encoding.UTF8, "application/json"),
            };
            using var response = await _sourceImportHttp.SendAsync(request);
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
                throw new InvalidOperationException(ReadApiError(body, "无法打开这个视频文件。"));
            using var result = JsonDocument.Parse(body);
            if (!result.RootElement.TryGetProperty("id", out var projectId) || string.IsNullOrWhiteSpace(projectId.GetString()))
                throw new InvalidOperationException("视频已打开，但没有获得项目编号。请重试。");
            _nativeImportEnabled = false;
            PostNativeMessage(new { type = "native-source-ready", projectId = projectId.GetString() });
        }
        catch (Exception exception)
        {
            AppendLog("原生视频导入失败：" + exception.Message);
            PostNativeMessage(new { type = "native-source-error", message = exception.Message });
        }
        finally
        {
            _sourceImportInProgress = false;
            _webView.AllowExternalDrop = _nativeImportEnabled;
        }
    }

    private static string ReadApiError(string body, string fallback)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            return document.RootElement.TryGetProperty("error", out var error) && !string.IsNullOrWhiteSpace(error.GetString())
                ? error.GetString()!
                : fallback;
        }
        catch (JsonException)
        {
            return fallback;
        }
    }

    private void PostNativeMessage(object message)
    {
        try
        {
            _webView.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(message));
        }
        catch (InvalidOperationException)
        {
            // The page may still be starting or already shutting down.
        }
    }

    private async void OnFormClosing(object? sender, FormClosingEventArgs eventArgs)
    {
        if (_allowClose) return;
        eventArgs.Cancel = true;
        if (_closeInProgress) return;
        _closeInProgress = true;
        _loadingPanel.Visible = true;
        _loadingLabel.Text = "正在保存并安全关闭…";
        _loadingPanel.BringToFront();
        try
        {
            if (_webView.CoreWebView2 is not null)
            {
                await _webView.CoreWebView2.ExecuteScriptAsync(
                    "window.textCutFlushSaves ? window.textCutFlushSaves() : true");
                // ExecuteScriptAsync starts the promise but does not guarantee
                // that its fetch requests have settled before returning.
                await Task.Delay(700);
            }
        }
        catch
        {
            // The regular 450 ms autosave remains the fallback.
        }
        ShutdownBackend();
        _allowClose = true;
        BeginInvoke(Close);
    }

    private void ShutdownBackend()
    {
        if (_backend is null) return;
        try
        {
            if (!_backend.HasExited && _backendUrl is not null && _desktopToken is not null)
            {
                using var request = new HttpRequestMessage(HttpMethod.Post, _backendUrl + "/api/desktop/shutdown");
                request.Headers.Add("X-TextCut-Desktop-Token", _desktopToken);
                _http.Send(request);
                if (!_backend.WaitForExit(4000))
                    _backend.Kill(entireProcessTree: true);
            }
        }
        catch
        {
            try
            {
                if (!_backend.HasExited) _backend.Kill(entireProcessTree: true);
            }
            catch
            {
                // Windows is already closing the process.
            }
        }
        finally
        {
            _backend.Dispose();
            _http.Dispose();
            _sourceImportHttp.Dispose();
        }
    }

    private static int GetAvailablePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }
}

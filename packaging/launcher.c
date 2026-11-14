/* PrintHub.exe — запускает встроенный Python без окна консоли. */
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, PWSTR cmdLine, int show) {
    wchar_t dir[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    wchar_t exe[MAX_PATH + 32], cwd[MAX_PATH + 32], line[8192];
    swprintf(exe, MAX_PATH + 32, L"%ls\\python\\pythonw.exe", dir);
    swprintf(cwd, MAX_PATH + 32, L"%ls\\app", dir);
    swprintf(line, 8192, L"\"%ls\" -m printhub.desktop %ls", exe, cmdLine ? cmdLine : L"");

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(exe, line, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, cwd, &si, &pi)) {
        wchar_t msg[1024];
        swprintf(msg, 1024, L"Не удалось запустить PrintHub (ошибка %lu).\nПереустановите программу.\n\n%ls",
                 GetLastError(), exe);
        MessageBoxW(NULL, msg, L"PrintHub", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}

// x86 WinMM forwarding loader for the retail Ace of Spades client.
// No remote process operations, executable byte patches, or background threads.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>
#include "generated_bootstrap.h"
#include "generated_exports.h"

static HMODULE g_self;
static HMODULE g_system;
static INIT_ONCE g_system_once = INIT_ONCE_STATIC_INIT;
static volatile LONG g_schedule_state; // 0: retry, 1: queued/disabled
static BOOL g_game;
extern "C" FARPROC g_functions[EXPORT_COUNT] = {};

typedef int (__cdecl *AddPending)(int (__cdecl *)(void*), void*);
typedef int (__cdecl *IsInitialized)(void);
typedef int (__cdecl *RunString)(const char*, void*);
typedef void (__cdecl *FetchError)(void**, void**, void**);
typedef void (__cdecl *RestoreError)(void*, void*, void*);
typedef void (__cdecl *ClearError)(void);

static int __cdecl RunBootstrap(void* context) {
    HMODULE python = static_cast<HMODULE>(context);
    RunString run = reinterpret_cast<RunString>(GetProcAddress(python, "PyRun_SimpleStringFlags"));
    FetchError fetch = reinterpret_cast<FetchError>(GetProcAddress(python, "PyErr_Fetch"));
    RestoreError restore = reinterpret_cast<RestoreError>(GetProcAddress(python, "PyErr_Restore"));
    ClearError clear = reinterpret_cast<ClearError>(GetProcAddress(python, "PyErr_Clear"));
    if (run && fetch && restore && clear) {
        void *type = NULL, *value = NULL, *trace = NULL;
        fetch(&type, &value, &trace);
        run(kBootstrap, NULL);
        clear();
        restore(type, value, trace);
    }
    return 0; // Never propagate a patch error into the game.
}

static void ScheduleBootstrap() {
    if (!g_game || InterlockedCompareExchange(&g_schedule_state, 1, 0) != 0)
        return;
    HMODULE python = GetModuleHandleW(L"python27.dll");
    if (python) {
        IsInitialized ready = reinterpret_cast<IsInitialized>(GetProcAddress(python, "Py_IsInitialized"));
        AddPending add = reinterpret_cast<AddPending>(GetProcAddress(python, "Py_AddPendingCall"));
        if (ready && add && ready() && add(RunBootstrap, python) == 0)
            return;
    }
    InterlockedExchange(&g_schedule_state, 0);
}

static BOOL CALLBACK LoadSystemWinmm(PINIT_ONCE, PVOID, PVOID*) {
    WCHAR path[MAX_PATH + 16];
    UINT length = GetSystemDirectoryW(path, MAX_PATH);
    if (!length || length >= MAX_PATH)
        return FALSE;
    wcscat_s(path, L"\\winmm.dll");
    // An absolute system path prevents resolving our own proxy recursively.
    g_system = LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    return g_system != NULL && g_system != g_self;
}

extern "C" void __cdecl ResolveExport(unsigned index) {
    DWORD last_error = GetLastError();
    ScheduleBootstrap();
    if (!InitOnceExecuteOnce(&g_system_once, LoadSystemWinmm, NULL, NULL))
        RaiseFailFastException(NULL, NULL, 0);
    FARPROC target = GetProcAddress(g_system, kExportNames[index] ?
                                   kExportNames[index] : MAKEINTRESOURCEA(kExportOrdinals[index]));
    if (!target) {
        // A called API missing from the real system DLL cannot be emulated.
        RaiseException(EXCEPTION_NONCONTINUABLE_EXCEPTION, EXCEPTION_NONCONTINUABLE, 0, NULL);
        TerminateProcess(GetCurrentProcess(), ERROR_PROC_NOT_FOUND);
    }
    InterlockedExchangePointer(reinterpret_cast<PVOID volatile*>(&g_functions[index]),
                               reinterpret_cast<PVOID>(target));
    SetLastError(last_error);
}

// Tail calls preserve the real ABI (including unknown/ordinal-only exports).
// Resolve only once; subsequent audio/timer calls are a pointer check and jump.
#define PROXY(index) \
    extern "C" __declspec(naked) void proxy_##index() { \
        __asm { cmp dword ptr [g_schedule_state], 0 } \
        __asm { je resolve } \
        __asm { cmp dword ptr [g_functions + index * 4], 0 } \
        __asm { jne ready } \
        __asm { resolve: } \
        __asm { pushfd } \
        __asm { pushad } \
        __asm { push index } \
        __asm { call ResolveExport } \
        __asm { add esp, 4 } \
        __asm { popad } \
        __asm { popfd } \
        __asm { ready: jmp dword ptr [g_functions + index * 4] } \
    }
#include "generated_stubs.inc"

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = instance;
        WCHAR path[MAX_PATH];
        DWORD length = GetModuleFileNameW(NULL, path, MAX_PATH);
        if (length && length < MAX_PATH) {
            const WCHAR* name = wcsrchr(path, L'\\');
            name = name ? name + 1 : path;
            g_game = _wcsicmp(name, L"aos.exe") == 0 || _wcsicmp(name, L"aos_demo.exe") == 0;
        }
        if (!g_game)
            InterlockedExchange(&g_schedule_state, 1);
        // CPython 2.7's pending-call queue only schedules work; it neither
        // executes Python nor waits for the GIL here. The callback executes
        // at the next main-thread bytecode checkpoint after native loading.
        // No LoadLibrary, file access, Python imports, or threads in DllMain.
        ScheduleBootstrap();
    }
    return TRUE;
}

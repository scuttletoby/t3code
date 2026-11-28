// Example placement factsCommand for Windows (fork-kit/placement-facts/README.md). On Windows T3
// runs only an .exe (a .cmd, .bat or script needs a shell), so this is C# source for the compiler
// that ships with Windows (.NET Framework 4, C# 5). Edit the settings below, then in PowerShell:
//   & "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /out:C:\t3-placement\facts.exe facts-windows.cs
// and put the .exe's absolute path in Settings, placement, factsCommand. Exit 0: free for work,
// each line printed one more label. Exit 1: not available, the first line of the error output says
// why. T3 stops it after 5 seconds and reads that as "not available".
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

static class PlacementFacts
{
    // ---- settings ---------------------------------------------------------------------------------
    // Programs that mean "in use, place no work here": process names without ".exe", as Task
    // Manager's Details tab shows them.
    static readonly string[] BusyProcesses = new string[] { };
    // true: not available while running on battery (a laptop away from power may sleep).
    static readonly bool BusyOnBattery = true;
    // Labels printed every time (the same as Settings' placement labels; either place works).
    static readonly string[] ExtraLabels = new string[] { };
    // ------------------------------------------------------------------------------------------------

    [StructLayout(LayoutKind.Sequential)]
    struct SystemPowerStatus
    {
        public byte ACLineStatus;
        public byte BatteryFlag;
        public byte BatteryLifePercent;
        public byte SystemStatusFlag;
        public int BatteryLifeTime;
        public int BatteryFullLifeTime;
    }

    [DllImport("kernel32.dll")]
    static extern bool GetSystemPowerStatus(out SystemPowerStatus status);

    static int Busy(string reason)
    {
        Console.Error.WriteLine(reason);
        return 1;
    }

    static bool Running(string name)
    {
        Process[] found = Process.GetProcessesByName(name);
        foreach (Process process in found) process.Dispose();
        return found.Length > 0;
    }

    // An NVIDIA GPU the driver can see: `nvidia-smi -L` lists it within 3 seconds.
    static bool HasNvidiaGpu()
    {
        string smi = Path.Combine(Environment.SystemDirectory, "nvidia-smi.exe");
        if (!File.Exists(smi)) return false;
        ProcessStartInfo start = new ProcessStartInfo(smi, "-L");
        start.UseShellExecute = false;
        start.RedirectStandardOutput = true;
        start.CreateNoWindow = true;
        try
        {
            using (Process smiRun = Process.Start(start))
            {
                // Read on another thread, so a GPU list that never ends cannot hold this past its wait.
                System.Threading.Tasks.Task<string> output = smiRun.StandardOutput.ReadToEndAsync();
                if (!smiRun.WaitForExit(3000))
                {
                    smiRun.Kill();
                    return false;
                }
                return smiRun.ExitCode == 0 && output.Wait(1000) && output.Result.StartsWith("GPU ");
            }
        }
        catch (Exception)
        {
            return false;
        }
    }

    static int Main()
    {
        string busyFile = Path.Combine(
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "t3-placement"),
            "busy");
        if (File.Exists(busyFile))
        {
            string reason = "";
            try
            {
                using (StreamReader reader = new StreamReader(busyFile)) reason = reader.ReadLine() ?? "";
            }
            catch (Exception) { }
            return Busy("busy: " + (reason.Trim() != "" ? reason.Trim() : busyFile + " is there"));
        }

        foreach (string name in BusyProcesses)
        {
            if (Running(name)) return Busy("busy: " + name + " is running");
        }

        SystemPowerStatus power;
        // ACLineStatus 0: on battery; BatteryFlag 128: no battery in this machine.
        if (BusyOnBattery && GetSystemPowerStatus(out power) && power.ACLineStatus == 0 && power.BatteryFlag != 128)
        {
            return Busy("busy: on battery");
        }

        foreach (string label in ExtraLabels) Console.Out.WriteLine(label);
        if (HasNvidiaGpu())
        {
            Console.Out.WriteLine("gpu");
            Console.Out.WriteLine("cuda");
        }
        if (Running("Runner.Listener")) Console.Out.WriteLine("ci-runner");
        return 0;
    }
}

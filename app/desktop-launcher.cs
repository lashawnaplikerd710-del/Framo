using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using System.Reflection;

[assembly: AssemblyTitle("Framo")]
[assembly: AssemblyProduct("Framo")]
[assembly: AssemblyDescription("Framo 创作工作台")]
[assembly: AssemblyVersion("0.2.0.0")]
[assembly: AssemblyFileVersion("0.2.0.0")]

internal static class DesktopLauncher
{
    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "python", "pythonw.exe");
        string entry = Path.Combine(root, "Launch-Desktop.pyw");
        if (!File.Exists(python) || !File.Exists(entry))
        {
            MessageBox.Show("请先完整解压工作台文件夹，再启动本程序。", "Framo", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 1;
        }
        try
        {
            Process.Start(new ProcessStartInfo {
                FileName = python,
                Arguments = "-B -X utf8 \"" + entry + "\"",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Normal
            });
            return 0;
        }
        catch
        {
            MessageBox.Show("启动失败，请检查解压目录和 python\\pythonw.exe 是否完整。", "Framo", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}

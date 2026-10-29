# How to export a world

Use these steps to move a world from a Windows PC to this server. For example, a world that you hosted from the game. The steps take about 10 minutes.

1. Close Project Zomboid. Close it fully. Do this before you make the zip.
2. Open File Explorer.
3. Type `%USERPROFILE%\Zomboid` in the address bar. Press Enter. This opens the folder `C:\Users\<your name>\Zomboid`.
4. Look in the `Server` folder. Find the file that ends in `.ini`. Its name without `.ini` is your server name.
5. Find these three items. Replace `<servername>` with your server name.
   - The folder `Saves\Multiplayer\<servername>`
   - The file `db\<servername>.db`
   - The file `Server\<servername>.ini`
6. If these files exist, find them too:
   - `Server\<servername>_SandboxVars.lua`
   - `Server\<servername>_spawnregions.lua`
   - `Server\<servername>_spawnpoints.lua`
7. Make a new folder on your desktop. Name it `Zomboid`.
8. Open the new `Zomboid` folder. Make three folders in it: `Saves\Multiplayer`, `db`, and `Server`.
   - Make `Saves`, then make `Multiplayer` inside it.
9. Copy each item into the matching folder. For example, copy the `.ini` file into `Zomboid\Server`.
10. Right-click the new `Zomboid` folder. Choose "Send to", then "Compressed (zipped) folder".
11. If you do not run the server, send the zip file to the server owner. Use a file-sharing link if the file is too big for email. Tell the owner the server name.

Do not send passwords with the zip file. The import removes the old server passwords, and the owner sets new ones.

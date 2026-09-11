const { execFileSync } = require('node:child_process');
const path = require('node:path');

module.exports = async context => {
  if (context.electronPlatformName === 'darwin') {
    // Clear Finder metadata after Electron extraction as well as after copying our files.
    execFileSync('xattr', ['-cr', path.join(context.appOutDir, context.packager.appInfo.productFilename + '.app')]);
  }
};

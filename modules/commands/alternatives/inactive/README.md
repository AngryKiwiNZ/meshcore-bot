# Inactive Alternative Plugins

This directory is for storing alternative plugin implementations that you want to keep but **not load**.

## Purpose

Use this directory to:
- Store alternative plugins you're not currently using
- Keep backup versions of plugins
- Organize plugins you might want to switch to later
- Store example or template plugins

## How It Works

- **Plugins in this directory are ignored** by the plugin loader
- They will not be automatically loaded or discovered
- They will not replace default plugins
- They will not be loaded even if configured in `[Plugin_Overrides]`

## Usage Examples

### Storing an Inactive Plugin

If you have an alternative weather plugin (`wx_international.py`) that you're not currently using:

1. Move it to this directory:
   ```bash
   mv modules/commands/alternatives/wx_international.py \
      modules/commands/alternatives/inactive/wx_international.py
   ```

2. The plugin will no longer be loaded

### Activating an Inactive Plugin

To use a plugin from this directory:

1. Move it back to the parent `alternatives/` directory:
   ```bash
   mv modules/commands/alternatives/inactive/wx_international.py \
      modules/commands/alternatives/wx_international.py
   ```

2. Configure it in `config.ini` if needed:
   ```ini
   [Plugin_Overrides]
   wx = wx_international
   ```

3. Restart the bot

## Best Practices

- Use this directory to organize plugins you might want to switch between
- Keep documentation in plugin files explaining when/why to use them
- Consider versioning or dating plugin names if you keep multiple versions
- Example: `wx_international_v1.py`, `wx_international_v2.py`


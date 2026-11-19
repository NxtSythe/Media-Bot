"""Admin commands are only for members with a role that has the Administrator permission."""
import discord

ADMIN_PERMS = discord.Permissions(administrator=True)
NO_ACCESS = "Only members with a role that has the **Administrator** permission can use this."


def is_admin(user) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


class AdminCog:
    """Mixin: every slash command in the cog is admin-only (checked at runtime too)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # discord.py cog hook
        return is_admin(interaction.user)

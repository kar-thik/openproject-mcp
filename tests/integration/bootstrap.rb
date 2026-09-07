# Only run inside the disposable container created by scripts/live_smoke.py.
require "json"
require "securerandom"

admin = User.find_by!(login: "admin")
User.current = admin
if defined?(Setting::WorkPackageMultipleVersions)
  Setting.work_package_multiple_versions = ENV.fetch("OP_SMOKE_VERSION_MODE", "multiple") != "single"
end
project = Project.new(name: "MCP smoke", identifier: "mcp-smoke", public: false)
project.workspace_type = "project" if project.respond_to?(:workspace_type=)
project.save!
type = Type.where(is_milestone: false).first!
project.types = [type]
project.enabled_module_names = %w[work_package_tracking meetings time_tracking]
project.save!
field = WorkPackageCustomField.create!(name: "Smoke marker", field_format: "string", is_for_all: true)
type.custom_fields << field
project.work_package_custom_fields << field unless project.work_package_custom_fields.include?(field)
versions = ["Smoke release A", "Smoke release B"].map do |name|
  Version.create!(project: project, name: name).id
end
outsider = User.create!(login: "mcp-outsider", firstname: "Smoke", lastname: "Outsider",
                        mail: "mcp-outsider@example.invalid", password: SecureRandom.hex(24) + "aA1!", status: 1)
# Read freshly generated plaintext; the database value is hashed on supported versions.
def api_token_for(user)
  token = Token::API.create!(user: user)
  token.respond_to?(:plain_value) ? token.plain_value : token.value
end
admin_token = api_token_for(admin)
outsider_token = api_token_for(outsider)
puts "MCP_SMOKE_FIXTURE=" + {
  project_id: project.id, type_id: type.id, version_ids: versions,
  custom_field: "customField#{field.id}", admin_token: admin_token,
  multiple_versions: !!(defined?(Setting::WorkPackageMultipleVersions) && Setting::WorkPackageMultipleVersions.active?),
  outsider_token: outsider_token, core_version: OpenProject::VERSION.to_s
}.to_json
